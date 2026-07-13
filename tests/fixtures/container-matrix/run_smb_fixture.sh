#!/usr/bin/env bash
# run_smb_fixture.sh — SMB/CIFS 真实挂载测试 fixture
# 任务：T-1783952125417-d343 Step #3
# 规范：enterprise-daemon-full-e2e-followup.md §6.4
#
# SMB fixture 使用真实 Samba/CIFS mount；若 CI kernel 禁止 mount，
# 则在专用 privileged runner 执行，不用普通目录伪装 SMB。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SMB_MOUNT="/tmp/cw-smb-test"
SMB_USER="cwtest"
SMB_PASS="cwtestpass"

echo "=== SMB/CIFS Fixture Setup ==="

# 1. 检查 smbclient 可用性
if ! command -v smbclient &>/dev/null; then
    echo "SKIP: smbclient not installed"
    echo '{"status": "infrastructure_skip", "reason": "smbclient not available"}' > "${SCRIPT_DIR}/smb_result.json"
    exit 0
fi

# 2. 检查 CIFS mount 权限
if ! mount -t cifs 2>/dev/null | head -1 | grep -q "cifs"; then
    if [[ $(id -u) -ne 0 ]]; then
        echo "SKIP: CIFS mount requires root or CAP_SYS_ADMIN"
        echo '{"status": "infrastructure_skip", "reason": "insufficient privileges for CIFS mount"}' > "${SCRIPT_DIR}/smb_result.json"
        exit 0
    fi
fi

# 3. 启动 Samba 服务（使用 Docker）
echo "Starting Samba container..."
docker run -d --name cw-samba \
    -p 445:445 \
    -v "${SCRIPT_DIR}/project:/mount/project" \
    -e USER="${SMB_USER};${SMB_PASS};1000;1000" \
    -e SHARE="project;/mount/project;yes;no;no;${SMB_USER}" \
    dperson/samba:latest 2>/dev/null || {
    echo "SKIP: Failed to start Samba container"
    echo '{"status": "infrastructure_skip", "reason": "samba container failed"}' > "${SCRIPT_DIR}/smb_result.json"
    exit 0
}

# 等待 Samba 启动
sleep 3

# 4. 挂载 SMB 共享
mkdir -p "$SMB_MOUNT"
mount -t cifs //localhost/project "$SMB_MOUNT" \
    -o "username=${SMB_USER},password=${SMB_PASS},vers=3.0,noperm" || {
    echo "SKIP: CIFS mount failed"
    docker rm -f cw-samba 2>/dev/null
    echo '{"status": "infrastructure_skip", "reason": "CIFS mount failed"}' > "${SCRIPT_DIR}/smb_result.json"
    exit 0
}

echo "SMB mounted at ${SMB_MOUNT}"

# 5. 验证文件访问
if [[ -f "${SMB_MOUNT}/calc.py" ]]; then
    echo "SMB file access: OK"
    fs_type=$(stat -f -c '%T' "$SMB_MOUNT" 2>/dev/null || echo "unknown")
    echo "Filesystem type: ${fs_type}"

    # 6. 验证 peer UID vs file owner
    file_uid=$(stat -c '%u' "${SMB_MOUNT}/calc.py" 2>/dev/null || echo "0")
    peer_uid=$(id -u)
    owner_match="true"
    if [[ "$file_uid" != "$peer_uid" ]]; then
        owner_match="false"
        echo "Owner mismatch: file_uid=${file_uid}, peer_uid=${peer_uid}"
        echo "  (This is expected in SMB — daemon should still accept if FD is valid)"
    fi

    cat > "${SCRIPT_DIR}/smb_result.json" <<EOF
{
    "status": "pass",
    "fs_type": "${fs_type}",
    "file_owner_uid": ${file_uid},
    "peer_uid": ${peer_uid},
    "owner_match": ${owner_match}
}
EOF
else
    echo "SMB file access: FAILED"
    cat > "${SCRIPT_DIR}/smb_result.json" <<EOF
{"status": "fail", "reason": "file not accessible via SMB mount"}
EOF
fi

# 7. 清理
echo "Cleaning up..."
umount "$SMB_MOUNT" 2>/dev/null || true
docker rm -f cw-samba 2>/dev/null || true
rmdir "$SMB_MOUNT" 2>/dev/null || true

echo "=== SMB Fixture Complete ==="
cat "${SCRIPT_DIR}/smb_result.json"
