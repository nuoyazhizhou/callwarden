"""Phase 8: systemd unit 测试

测试 cicd/systemd_unit.py 的 unit 生成、验证和部署脚本。

测试内容：
- generate_systemd_unit 返回包含必要段的字符串
- validate_unit_content 检查所有关键字段
- generate_deploy_script 返回有效 bash 脚本
- install_systemd_unit 返回正确路径
- 参数定制（user/port/memory/cpu）
"""

import os

import pytest

from callwarden.cicd.systemd_unit import (
    generate_systemd_unit,
    install_systemd_unit,
    generate_deploy_script,
    validate_unit_content,
)


# ============================================
# generate_systemd_unit 测试
# ============================================

class TestGenerateSystemdUnit:
    def test_returns_string(self):
        """生成结果为字符串"""
        content = generate_systemd_unit()
        assert isinstance(content, str)
        assert len(content) > 0

    def test_has_three_sections(self):
        """包含 [Unit] [Service] [Install] 三个段"""
        content = generate_systemd_unit()
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content

    def test_has_exec_start(self):
        """包含 ExecStart 指令"""
        content = generate_systemd_unit()
        assert "ExecStart=" in content
        assert "cw.py server" in content
        assert "--transport sse" in content

    def test_has_restart_policy(self):
        """包含 Restart 策略"""
        content = generate_systemd_unit()
        assert "Restart=on-failure" in content
        assert "RestartSec=" in content

    def test_has_resource_limits(self):
        """包含内存和 CPU 限制"""
        content = generate_systemd_unit()
        assert "MemoryMax=" in content
        assert "CPUQuota=" in content

    def test_has_user_and_group(self):
        """包含 User 和 Group 指令"""
        content = generate_systemd_unit()
        assert "User=callwarden" in content
        assert "Group=callwarden" in content

    def test_has_security_hardening(self):
        """包含安全加固指令"""
        content = generate_systemd_unit()
        assert "NoNewPrivileges=true" in content
        assert "PrivateTmp=true" in content

    def test_custom_port(self):
        """自定义端口出现在 ExecStart 和 Environment 中"""
        content = generate_systemd_unit(port=9999)
        assert "--port 9999" in content
        assert "CW_SSE_PORT=9999" in content

    def test_custom_memory(self):
        """自定义内存限制"""
        content = generate_systemd_unit(memory_max="2G")
        assert "MemoryMax=2G" in content

    def test_custom_cpu(self):
        """自定义 CPU 配额"""
        content = generate_systemd_unit(cpu_quota="300%")
        assert "CPUQuota=300%" in content

    def test_custom_user(self):
        """自定义用户"""
        content = generate_systemd_unit(user="myuser", group="mygroup")
        assert "User=myuser" in content
        assert "Group=mygroup" in content

    def test_custom_working_dir(self):
        """自定义工作目录"""
        content = generate_systemd_unit(working_dir="/home/cw")
        assert "WorkingDirectory=/home/cw" in content
        assert "/home/cw/cw.py" in content

    def test_db_path_env(self):
        """db_path 出现在 Environment 中"""
        content = generate_systemd_unit(db_path="/data/cw.db")
        assert "CW_DB_PATH=/data/cw.db" in content

    def test_workspace_root_env(self):
        """workspace_root 出现在 Environment 中"""
        content = generate_systemd_unit(workspace_root="/repos/myproject")
        assert "CW_WORKSPACE_ROOT=/repos/myproject" in content

    def test_after_network_target(self):
        """After=network.target 确保网络就绪"""
        content = generate_systemd_unit()
        assert "After=network.target" in content

    def test_wanted_by_multi_user(self):
        """WantedBy=multi-user.target"""
        content = generate_systemd_unit()
        assert "WantedBy=multi-user.target" in content

    def test_kill_signal_sigterm(self):
        """KillSignal=SIGTERM 优雅停止"""
        content = generate_systemd_unit()
        assert "KillSignal=SIGTERM" in content
        assert "TimeoutStopSec=" in content

    def test_start_limit(self):
        """包含启动限流（StartLimitBurst）"""
        content = generate_systemd_unit()
        assert "StartLimitInterval=" in content
        assert "StartLimitBurst=" in content


# ============================================
# validate_unit_content 测试
# ============================================

class TestValidateUnitContent:
    def test_all_checks_pass_for_generated(self):
        """生成的 unit 内容通过所有验证"""
        content = generate_systemd_unit()
        checks = validate_unit_content(content)
        assert all(checks.values()), f"Failed checks: {checks}"

    def test_missing_section_detected(self):
        """缺少段时对应检查为 False"""
        content = "ExecStart=/bin/true"
        checks = validate_unit_content(content)
        assert not checks["has_unit_section"]
        assert not checks["has_install_section"]

    def test_missing_memory_limit(self):
        """缺少内存限制时 has_memory_limit=False"""
        content = """[Unit]
Description=test

[Service]
ExecStart=/bin/true
Restart=on-failure
User=test

[Install]
WantedBy=multi-user.target
"""
        checks = validate_unit_content(content)
        assert not checks["has_memory_limit"]
        assert not checks["has_cpu_limit"]
        assert checks["has_exec_start"]
        assert checks["has_restart"]

    def test_missing_security(self):
        """缺少安全加固时 has_security=False"""
        content = """[Unit]
Description=test

[Service]
ExecStart=/bin/true
Restart=on-failure
User=test
MemoryMax=1G
CPUQuota=100%

[Install]
WantedBy=multi-user.target
"""
        checks = validate_unit_content(content)
        assert not checks["has_security"]
        assert checks["has_memory_limit"]

    def test_returns_dict(self):
        """返回 dict 类型"""
        checks = validate_unit_content(generate_systemd_unit())
        assert isinstance(checks, dict)
        assert len(checks) == 9


# ============================================
# generate_deploy_script 测试
# ============================================

class TestGenerateDeployScript:
    def test_returns_string(self):
        """返回字符串"""
        script = generate_deploy_script()
        assert isinstance(script, str)
        assert len(script) > 0

    def test_has_shebang(self):
        """包含 bash shebang"""
        script = generate_deploy_script()
        assert script.startswith("#!/bin/bash")

    def test_creates_user(self):
        """包含创建用户命令"""
        script = generate_deploy_script()
        assert "useradd" in script
        assert "callwarden" in script

    def test_installs_unit(self):
        """包含安装 unit 命令"""
        script = generate_deploy_script()
        assert "systemctl daemon-reload" in script
        assert "systemctl enable" in script
        assert "systemctl start" in script

    def test_custom_working_dir(self):
        """自定义工作目录"""
        script = generate_deploy_script(working_dir="/opt/cw")
        assert "/opt/cw" in script

    def test_has_chown(self):
        """包含 chown 设置权限"""
        script = generate_deploy_script()
        assert "chown" in script
        assert "callwarden:callwarden" in script

    def test_has_status_check(self):
        """包含状态检查"""
        script = generate_deploy_script()
        assert "systemctl status" in script
        assert "journalctl" in script


# ============================================
# install_systemd_unit 测试
# ============================================

class TestInstallSystemdUnit:
    def test_returns_path(self):
        """返回安装路径"""
        content = generate_systemd_unit()
        path = install_systemd_unit(content)
        assert isinstance(path, str)
        assert "callwarden.service" in path

    def test_custom_install_dir(self):
        """自定义安装目录"""
        content = generate_systemd_unit()
        path = install_systemd_unit(content, install_dir="/tmp/test")
        assert "/tmp/test" in path
        assert "callwarden.service" in path

    def test_custom_unit_name(self):
        """自定义 unit 名称"""
        content = generate_systemd_unit()
        path = install_systemd_unit(content, unit_name="my-cw.service")
        assert "my-cw.service" in path
