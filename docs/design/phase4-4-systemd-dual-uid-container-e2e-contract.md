# Phase 4-4 契约：systemd、双 UID、容器挂载与真实 Linux E2E

**Task ID**: `T-1785231817523-a391094e`（Phase 4-4）
**状态**: contract
**日期**: 2026-07-28
**验证环境**: WSL2 (Ubuntu 22.04.5 LTS, kernel 6.18.33.2-microsoft-standard-WSL2)

## 1. 范围

Phase 4-4 是 **Linux 特定的端到端验证阶段**，不新增 PyO3 API（UID/ACL/路径校验已在 Phase 4-2 完成）。重点是真实 Linux（WSL）环境下的 systemd 部署、双 UID SO_PEERCRED ACL、容器挂载场景验证。

**涉及**：
- **systemd 部署 E2E**：`Type=notify` + `sd_notify(READY=1)` + `ExecStartPre schema-check` + 信号处理（SIGHUP/SIGUSR1/SIGTERM）
- **双 UID ACL E2E**：真实 root + callwarden 用户 + 普通用户三方场景，验证 SO_PEERCRED 不可伪造性
- **容器挂载 E2E**：bind mount 路径下宿主 agent 观察，SMB/CIFS mount 的 owner mismatch 审计
- **WSL 环境验证**：在 Windows 主机的 WSL2 中执行上述所有 E2E 场景

**不涉及**（已在 Phase 4-1/4-2/4-3 完成）：
- 新增 PyO3 API
- 新增 Rust daemon 模块
- 新增 wire-production 接入点
- 新增 rollback_config 登记项
- Windows 环境验证

## 2. 现有资产盘点

### 2.1 systemd 资产（已实现）

| 资产 | 路径 | 说明 |
|---|---|---|
| CI/CD unit | `cicd/callwarden-daemon.service` | Type=notify, User=callwarden, MemoryHigh=2G, MemoryMax=4G |
| DEB unit | `release/linux/deb/systemd/callwarden-daemon.service` | ExecStartPre=/usr/bin/cw-daemon schema-check |
| agent user unit | `release/linux/deb/systemd/callwarden-agent.service` | per-UID `systemctl --user` unit |
| sysusers.d | `release/linux/deb/sysusers.d/callwarden.conf` | 系统用户声明 |
| tmpfiles.d | `release/linux/deb/tmpfiles.d/callwarden.conf` | 运行时目录声明 |
| unit 生成器 | `cicd/systemd_unit.py` | `generate_enterprise_daemon_unit()` / `validate_unit_content()` |
| Rust binary | `rust_ext/src/bin/cw_daemon.rs` | sd_notify 集成 + 信号处理 |
| 部署手册 | `docs/design/daemon-deploy-runbook.md` | 9 节部署/升级/回滚手册 |

### 2.2 双 UID ACL 资产（Phase 4-2 已完成 wire-production）

| 资产 | 路径 | 说明 |
|---|---|---|
| Rust peercred | `rust_ext/src/daemon/peercred.rs` | `PeerCred { uid, gid, pid }` + SO_PEERCRED + LOCAL_PEERCRED |
| Rust dispatch | `rust_ext/src/daemon/dispatch.rs` | `ADMIN_ONLY_METHODS` (14 个) + `is_admin(peer)` + `current_daemon_uid()` |
| Rust PyO3 暴露 | `rust_ext/src/daemon_query.rs` | 5 个 ACL API（validate_owned_path / check_path_within_workspace / is_admin_uid / current_daemon_uid_py / check_workspace_owner） |
| Python wire | `server/daemon_server.py` | `_is_admin_peer()` / `_validate_owned_path()` / `_owned_workspace()` Rust 短路 + fail-soft 降级 |
| rollback 登记 | rollback_config 表 | `rust_daemon_acl_path_budget` (Phase 4-2) |
| 差分测试 | `tests/test_phase4_2_acl_path_budget_diff.py` | D1-D6 矩阵，~30 用例 |
| dual UID 脚本 | `scripts/test_enterprise_daemon_dual_uid.sh` | Linux root 真实验收脚本 |

### 2.3 容器挂载资产（已实现）

| 资产 | 路径 | 说明 |
|---|---|---|
| 容器矩阵 | `tests/fixtures/container-matrix/docker-compose.yml` | 6 个 Ubuntu 版本 (14.04/16.04/18.04/20.04/22.04/24.04) |
| 矩阵脚本 | `tests/fixtures/container-matrix/run_container_matrix.sh` | 串行测试脚本 |
| SMB fixture | `tests/fixtures/container-matrix/run_smb_fixture.sh` | 真实 Samba mount |
| VS Code fixture | `tests/fixtures/container-matrix/run_vscode_remote_test.sh` | VS Code Remote 场景 |
| mount RPC | `server/daemon_server.py` | `mount.register/list/delete` (admin-only) |
| mount DB | `db/db_daemon.py` | `register_mount_mapping` 等 |
| 容器 ADR | `docs/design/adr-001-legacy-container-client.md` | 宿主 agent + 容器只被观察 |
| E2E 设计 | `docs/design/enterprise-daemon-full-e2e-followup.md` | §6 容器/SMB/VS Code 矩阵 |

### 2.4 E2E 测试资产（已实现）

| 资产 | 说明 |
|---|---|
| `tests/test_enterprise_daemon_uds.py` | UDS + SO_PEERCRED + SCM_RIGHTS E2E |
| `tests/test_phase8_admin_rpc_authz.py` | 14 个 admin 方法授权矩阵 |
| `tests/test_phase5_container_matrix.py` | 容器/SMB/VS Code E2E |
| `tests/test_p1_f_kill9_stale_e2e.py` | kill -9 崩溃恢复 E2E |
| `.github/workflows/e2e-verify-linux-x86_64.yml` | CI E2E workflow |

## 3. E2E 验证矩阵

### 3.1 D1: systemd 部署 E2E

| 场景 | 输入 | 期望行为 | WSL 验证方式 |
|---|---|---|---|
| D1.1 | `systemctl start callwarden-daemon` | daemon 发送 READY=1，systemd 标记 active | `systemctl status` 显示 active (running) |
| D1.2 | daemon 启动失败（schema 损坏） | ExecStartPre 返回非零，systemd 标记 failed | `systemctl status` 显示 failed |
| D1.3 | `kill -TERM <daemon_pid>` | daemon 收到 SIGTERM，发送 STOPPING=1，优雅关闭 | `systemctl status` 显示 inactive (dead) |
| D1.4 | `kill -HUP <daemon_pid>` | daemon 收到 SIGHUP，重新加载配置 | 配置项更新生效 |
| D1.5 | `kill -USR1 <daemon_pid>` | daemon 收到 SIGUSR1，drain staging logs | staging.log 中 pending entries 被处理 |
| D1.6 | daemon 崩溃（`kill -9`） | systemd 自动 restart（Restart=on-failure） | `systemctl status` 显示新 PID，日志有 recovery 记录 |
| D1.7 | `systemctl --user enable callwarden-agent` | per-UID agent 在用户登录时启动 | `loginctl enable-linger` + `systemctl --user status` |

### 3.2 D2: 双 UID SO_PEERCRED ACL E2E

| 场景 | 客户端 UID | 目标方法 | 期望行为 |
|---|---|---|---|
| D2.1 | root (uid=0) | `backup` (admin-only) | ✅ 允许（root 是 admin） |
| D2.2 | callwarden (daemon self) | `backup` | ✅ 允许（daemon self 是 admin） |
| D2.3 | 普通用户 A (uid=1000) | `backup` | ❌ `permission_denied` |
| D2.4 | 普通用户 A | `query` (workspace A owned) | ✅ 允许（owner） |
| D2.5 | 普通用户 A | `query` (workspace B owned) | ❌ `workspace_not_owned` |
| D2.6 | 普通用户 A | `ping` | ✅ 允许（非 admin 方法） |
| D2.7 | 伪造请求体 uid=0 | `backup` | ❌ `permission_denied`（SO_PEERCRED 不可伪造） |
| D2.8 | 普通用户 A 访问路径 `/workspace_B/file.py` | `query` | ❌ `path_not_owned` |
| D2.9 | 普通用户 A 传入 SCM_RIGHTS FD（属主=B） | `snapshot.read` | ❌ `fd_owner_mismatch` |

### 3.3 D3: 容器挂载 E2E

| 场景 | 客户端 | 目标 | 期望行为 |
|---|---|---|---|
| D3.1 | ubuntu-2204 容器（agent-mode=container-capable） | bind mount 路径 `/project/calc.py` | ✅ daemon 通过宿主 agent 观察到文件变更 |
| D3.2 | ubuntu-1404 容器（agent-mode=host-only） | `/project/calc.py` | ✅ 宿主 agent 处理，容器无 Python agent |
| D3.3 | SMB/CIFS mount | uid mismatch 文件 | ✅ daemon 记录 audit 日志，不作为统一授权依据 |
| D3.4 | VS Code Remote | `CW_DAEMON_SOCKET` 环境变量 | ✅ 自动发现 UDS，切换 workspace |
| D3.5 | 普通用户尝试 `mount.register` | admin-only 方法 | ❌ `permission_denied` |
| D3.6 | 容器内绝对路径 `/etc/passwd` | `file_read` | ❌ `path_outside_workspace` |

### 3.4 D4: WSL 环境验证

| 场景 | 验证点 | 期望行为 |
|---|---|---|
| D4.1 | WSL2 kernel 6.18.33.2 | memfd_create 可用（Linux 3.17+） |
| D4.2 | Ubuntu 22.04.5 systemd | `systemctl` 命令可用，systemd 是 PID 1 |
| D4.3 | root 用户（uid=0） | 可启动 daemon，可访问 admin 方法 |
| D4.4 | `/run/callwarden/` UDS socket | 0660 权限，属主 callwarden:callwarden-clients |
| D4.5 | 跨 WSL/Windows 文件系统访问 | `/mnt/c/git_work/callwarden` 可读写 |

## 4. WSL 验证流程

### 4.1 环境准备

```bash
# 1. 确认 WSL2 + Ubuntu 22.04
wsl -- bash -lc "uname -r; cat /etc/os-release | head -3"

# 2. 确认 systemd 可用
wsl -- bash -lc "ps -p 1 -o comm=; which systemctl"

# 3. 安装 Python 3 + pip + venv（AGENTS.md 规则 19）
wsl -- bash -lc "python3 --version; python3 -m pip --version; python3 -m venv --help | head -1"

# 4. 创建 callwarden 系统用户
wsl -- bash -lc "useradd -r -m -d /var/lib/callwarden -s /usr/sbin/nologin callwarden 2>/dev/null || echo 'user exists'"

# 5. 创建运行时目录
wsl -- bash -lc "mkdir -p /run/callwarden /var/lib/callwarden /var/log/callwarden && chown callwarden:callwarden /run/callwarden /var/lib/callwarden /var/log/callwarden"
```

### 4.2 daemon 部署与验证

```bash
# 1. 编译 Rust daemon binary（Linux target）
wsl -- bash -lc "cd /mnt/c/git_work/callwarden/rust_ext && cargo build --no-default-features --release --bin cw-daemon"

# 2. 安装 systemd unit
wsl -- bash -lc "cp /mnt/c/git_work/callwarden/cicd/callwarden-daemon.service /etc/systemd/system/ && systemctl daemon-reload"

# 3. 启动 daemon
wsl -- bash -lc "systemctl start callwarden-daemon && systemctl status callwarden-daemon --no-pager"

# 4. 验证 sd_notify READY=1
wsl -- bash -lc "systemctl is-active callwarden-daemon"  # 应输出 "active"

# 5. 信号测试
wsl -- bash -lc "systemctl kill callwarden-daemon --signal=SIGHUP && journalctl -u callwarden-daemon -n 10 --no-pager"
wsl -- bash -lc "systemctl kill callwarden-daemon --signal=SIGUSR1 && journalctl -u callwarden-daemon -n 10 --no-pager"
wsl -- bash -lc "systemctl stop callwarden-daemon"
```

### 4.3 双 UID ACL 验证

```bash
# 1. 创建普通用户 A 和 B
wsl -- bash -lc "useradd -m user_a 2>/dev/null; useradd -m user_b 2>/dev/null"

# 2. 启动 daemon（以 callwarden 用户）
wsl -- bash -lc "systemctl start callwarden-daemon"

# 3. root 访问 admin 方法（应允许）
wsl -- bash -lc "python3 /mnt/c/git_work/callwarden/cw.py daemon ping"

# 4. 普通用户 A 访问 admin 方法（应拒绝）
wsl -- bash -lc "su - user_a -c 'python3 /mnt/c/git_work/callwarden/cw.py daemon backup'"

# 5. 普通用户 A 注册 workspace（应允许）
wsl -- bash -lc "su - user_a -c 'python3 /mnt/c/git_work/callwarden/cw.py daemon register --root /home/user_a/project'"

# 6. 普通用户 B 访问 A 的 workspace（应拒绝）
wsl -- bash -lc "su - user_b -c 'python3 /mnt/c/git_work/callwarden/cw.py daemon query --workspace-id 1'"
```

### 4.4 容器挂载验证（可选，需 Docker）

```bash
# 1. 确认 Docker 可用
wsl -- bash -lc "docker --version"

# 2. 启动容器矩阵
wsl -- bash -lc "cd /mnt/c/git_work/callwarden/tests/fixtures/container-matrix && docker-compose up -d"

# 3. 在容器内测试文件访问
wsl -- bash -lc "docker exec ubuntu-2204 cat /project/calc.py"

# 4. 验证宿主 agent 观察到文件变更
wsl -- bash -lc "docker exec ubuntu-2204 bash -c 'echo \"# modified\" >> /project/calc.py'"
wsl -- bash -lc "journalctl -u callwarden-daemon -n 20 --no-pager | grep 'file_changed'"
```

## 5. 预期差异

### 5.1 WSL2 vs 真实 Linux 差异

| 维度 | WSL2 | 真实 Linux | 影响 |
|---|---|---|---|
| 内核 | 6.18.33.2-microsoft-standard-WSL2 | 原生 kernel | memfd_create 可用（3.17+） |
| systemd | WSL2 自 2022 年支持 | 原生 systemd | 行为一致 |
| 文件系统 | 9p 协议访问 `/mnt/c/` | ext4 | 跨 WSL/Windows 文件访问性能略低，但功能一致 |
| 权限 | root 默认 | 需配置 | WSL 默认 root，需手动创建 callwarden 用户 |
| Docker | WSL2 backend | 原生 | 行为一致 |

### 5.2 不可验证的场景

1. **真实多用户登录**：WSL 默认单用户会话，`loginctl enable-linger` + `systemctl --user` 行为可能与真实多用户系统略有差异
2. **cgroup v2 资源限制**：WSL2 的 cgroup 层级可能与原生 Linux 不同，`MemoryHigh` / `MemoryMax` 的强制行为需在真实 Linux 验证
3. **网络命名空间**：WSL2 使用 NAT 网络，UDS（Unix Domain Socket）不受影响，但网络相关 RPC 行为可能不同

## 6. 实现计划

### P0: 环境准备与契约（当前）

1. **编写本契约文档** ✅
2. **WSL 环境探测**：确认 systemd / Python / venv / Docker 可用性
3. **创建 callwarden 系统用户**

### P1: systemd 部署 E2E

1. **编译 Rust daemon binary**（Linux target）
2. **安装 systemd unit 到 `/etc/systemd/system/`**
3. **执行 D1.1-D1.7 验证矩阵**
4. **记录验证结果到 migration-manifest.md**

### P2: 双 UID ACL E2E

1. **创建普通用户 A 和 B**
2. **启动 daemon（callwarden 用户）**
3. **执行 D2.1-D2.9 验证矩阵**
4. **记录验证结果**

### P3: 容器挂载 E2E（可选，需 Docker）

1. **启动容器矩阵**（docker-compose）
2. **执行 D3.1-D3.6 验证矩阵**
3. **SMB/CIFS mount 验证**（若 samba 可用）
4. **记录验证结果**

### P4: WSL 环境验证

1. **执行 D4.1-D4.5 验证矩阵**
2. **记录 WSL 特有差异**
3. **更新 migration-manifest.md §31 Review 清单**

## 7. 验收标准

1. **systemd D1.1-D1.7 全部通过**：daemon 可通过 `systemctl start/stop/restart` 管理，信号处理正确
2. **双 UID D2.1-D2.9 全部通过**：SO_PEERCRED ACL 正确，admin 方法仅 root/daemon self 可访问
3. **容器 D3.1-D3.6 全部通过**（若 Docker 可用）：宿主 agent 观察正确，路径校验严格
4. **WSL D4.1-D4.5 全部通过**：WSL2 环境满足所有验证前提
5. **migration-manifest.md §31 Review 清单完整**：交付物、验证结果、关键点、风险注意事项齐全
6. **迁移状态跟踪表 Phase 4-4 行更新**：contract/implement/differential-test/wire-production/verify/refresh 状态为 ✅，review 为 ⏸️

## 8. 风险与注意事项

### 8.1 AGENTS.md 强制规则

- **规则 19**：WSL 验收先检查 `python3 -m pip --version` / `python3 -m venv --help` / `import pytest`；缺少 venv 支持时先安装 `python3-venv`
- **规则 20**：PowerShell 调 WSL 避免嵌套代码字符串；拆成独立简单命令
- **规则 23**：TRAE 沙箱拦截 sh.exe 子进程对 `~/.callwarden/` 的写操作；Linux 验收需在 WSL 中独立执行
- **规则 24**：Rust daemon ACL 变更必须跑完整 daemon 测试集（本阶段不修改 ACL，仅验证）
- **规则 28**：文件清理必须单 shell、单文件、绝对字面路径

### 8.2 Linux 环境特定风险

1. **真实 root 权限**：dual UID 脚本强制要求 root；WSL 默认 root，可直接验证
2. **memfd Linux 3.17+**：WSL2 内核 6.18 满足，无需 fallback
3. **SO_PEERCRED 不可伪造**：客户端请求体中的 UID 字段不参与授权，只看 kernel 返回的 `ucred.uid`
4. **socket 权限 0660**：非 callwarden 用户需加入 `callwarden-clients` 组才能访问 UDS
5. **schema-check 在 fresh install**：P0-4 修复已移除 `--strict`，Phase 4-4 验证不回退此修复

### 8.3 容器/SMB 特定风险

1. **SMB owner mismatch**：CIFS/SMB mount uid 可能与连接 UID 不一致；`st_uid == peer_uid` 只能作为审计信息
2. **client_view_root 不可作为授权依据**：daemon 不得调用 `open(client_abs_path)`
3. **mount.list 是 admin-only**：`container_mount_mappings` 表无 owner_uid 列
4. **容器 bind mount 路径权限**：ubuntu-1404/1604/1804 用 `:ro` 只读挂载

### 8.4 文档同步规则（AGENTS.md 规则 22）

Phase 4-4 完成后需同步更新：
- `docs/design/migration-manifest.md` — 新增 §31 Phase 4-4 Review 清单
- `docs/design/implementation-status.md` — 更新 Phase 4 状态
- `docs/design/phase-spec-cross-reference.md` — 补充 Phase 4-4 短规范映射
- 不涉及 MCP 工具/CLI/Schema 数量变化（纯验证阶段）

## 9. 与 Phase 4-1/4-2/4-3 的关系

| Phase | 交付物 | Phase 4-4 关系 |
|---|---|---|
| 4-1 | UDS framing + RPC dispatch | 提供 UDS 通信基础，Phase 4-4 验证其在 Linux 真实运行 |
| 4-2 | UID/workspace ACL + 路径安全 + 资源预算 | 提供 ACL 能力，Phase 4-4 验证其在真实双 UID 场景下生效 |
| 4-3 | metrics/health/audit/backup 纯计算 | 提供运维能力，Phase 4-4 验证 systemd 集成下可用 |
| **4-4** | **E2E 验证 + 契约文档** | **不新增功能，验证 Phase 4-1/4-2/4-3 在真实 Linux 环境下端到端可用** |

## 10. 下一步

Phase 4-4 完成后，Phase 4 全部子任务收尾。下一步推进：
- **Phase 5**: Rust CLI 命令树与配置加载
- **Phase 6**: 文档与示例
- **Phase 7**: 清理 rollback_config（rollback_window_until 过期后删除 rollback_entry）
