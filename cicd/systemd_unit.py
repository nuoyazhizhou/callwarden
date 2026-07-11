"""Phase 8: systemd unit 生成与管理

为 Call Warden MCP Server 生成 systemd unit 文件，支持在共享 Linux 开发机上
长期运行。daemon restart 后自动恢复 workspace registry 和 snapshots。

生成的 unit 文件包含：
- ExecStart: cw server --transport sse
- Restart=on-failure + RestartSec=5
- 用户/组配置
- 内存限制（MemoryMax）
- CPU 限制（CPUQuota）
- 工作目录和环境变量
"""

from __future__ import annotations

import os
from typing import Dict, Optional


def generate_systemd_unit(
    user: str = "callwarden",
    group: str = "callwarden",
    working_dir: str = "/opt/callwarden",
    db_path: str = "",
    workspace_root: str = "",
    port: int = 8765,
    memory_max: str = "1G",
    cpu_quota: str = "200%",
    restart_sec: int = 5,
    log_level: str = "INFO",
) -> str:
    """生成 systemd unit 文件内容

    Args:
        user: 运行用户
        group: 运行组
        working_dir: 工作目录
        db_path: 数据库路径（留空使用默认 ~/.callwarden/）
        workspace_root: 默认 workspace 根路径
        port: SSE 监听端口
        memory_max: 内存上限（systemd MemoryMax 格式，如 "1G"）
        cpu_quota: CPU 配额（systemd CPUQuota 格式，如 "200%" 表示 2 核）
        restart_sec: 重启间隔秒数
        log_level: 日志级别

    Returns:
        systemd unit 文件内容字符串
    """
    env_lines = [
        f"Environment=CW_SSE_PORT={port}",
        f"Environment=CW_LOG_LEVEL={log_level}",
    ]
    if db_path:
        env_lines.append(f"Environment=CW_DB_PATH={db_path}")
    if workspace_root:
        env_lines.append(f"Environment=CW_WORKSPACE_ROOT={workspace_root}")

    env_block = "\n".join(env_lines)

    return f"""[Unit]
Description=Call Warden MCP Server (Code Knowledge Graph)
Documentation=file:///opt/callwarden/docs/quickstart.md
After=network.target
Wants=network.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={working_dir}
ExecStart={working_dir}/cw.py server --transport sse --port {port}
Restart=on-failure
RestartSec={restart_sec}
StartLimitInterval=60
StartLimitBurst=3
KillSignal=SIGTERM
TimeoutStopSec=30

# 资源限制
MemoryMax={memory_max}
CPUQuota={cpu_quota}

# 环境
{env_block}

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={working_dir} /tmp

[Install]
WantedBy=multi-user.target
"""


def install_systemd_unit(
    unit_content: str,
    unit_name: str = "callwarden.service",
    install_dir: str = "/etc/systemd/system",
) -> str:
    """安装 systemd unit 文件

    Args:
        unit_content: unit 文件内容
        unit_name: unit 文件名
        install_dir: 安装目录

    Returns:
        安装后的文件路径
    """
    # 在实际环境中需要 root 权限写入 /etc/systemd/system/
    # 这里只生成路径，实际写入由部署脚本完成
    unit_path = os.path.join(install_dir, unit_name)
    return unit_path


def generate_deploy_script(
    unit_name: str = "callwarden.service",
    working_dir: str = "/opt/callwarden",
) -> str:
    """生成部署脚本内容

    包含安装 unit、启动服务、查看状态的命令

    Args:
        unit_name: unit 文件名
        working_dir: 工作目录

    Returns:
        部署脚本内容（bash）
    """
    return f"""#!/bin/bash
# Call Warden daemon 部署脚本
# 使用：sudo bash deploy.sh

set -e

UNIT_NAME="{unit_name}"
WORKING_DIR="{working_dir}"
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"

# 1. 创建用户和组（如果不存在）
if ! id "callwarden" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin callwarden
fi

# 2. 创建工作目录
mkdir -p "$WORKING_DIR"
chown -R callwarden:callwarden "$WORKING_DIR"

# 3. 安装 unit 文件
cp "$UNIT_NAME" "$UNIT_PATH"
chown root:root "$UNIT_PATH"
chmod 644 "$UNIT_PATH"

# 4. 重载 systemd 配置
systemctl daemon-reload

# 5. 启用并启动
systemctl enable "$UNIT_NAME"
systemctl start "$UNIT_NAME"

# 6. 查看状态
systemctl status "$UNIT_NAME"

echo "Call Warden daemon deployed and started."
echo "Check logs: journalctl -u $UNIT_NAME -f"
"""


def validate_unit_content(unit_content: str) -> Dict[str, bool]:
    """验证 systemd unit 文件内容的基本正确性

    Args:
        unit_content: unit 文件内容

    Returns:
        各项检查结果的 dict：
        - has_unit_section: 是否包含 [Unit] 段
        - has_service_section: 是否包含 [Service] 段
        - has_install_section: 是否包含 [Install] 段
        - has_exec_start: 是否包含 ExecStart
        - has_restart: 是否包含 Restart 策略
        - has_memory_limit: 是否包含内存限制
        - has_cpu_limit: 是否包含 CPU 限制
        - has_user: 是否包含 User 指令
        - has_security: 是否包含安全加固
    """
    checks = {
        "has_unit_section": "[Unit]" in unit_content,
        "has_service_section": "[Service]" in unit_content,
        "has_install_section": "[Install]" in unit_content,
        "has_exec_start": "ExecStart=" in unit_content,
        "has_restart": "Restart=" in unit_content,
        "has_memory_limit": "MemoryMax=" in unit_content,
        "has_cpu_limit": "CPUQuota=" in unit_content,
        "has_user": "User=" in unit_content,
        "has_security": "NoNewPrivileges=" in unit_content,
    }
    return checks
