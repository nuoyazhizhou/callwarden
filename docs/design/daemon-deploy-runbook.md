# Call Warden Enterprise Daemon — 部署/升级/回滚 Runbook

> 任务：T-1784351992060-754393b5（修订 cw_daemon systemd unit + 部署文档）
> 原始任务：T-1783974522652-e0c7 Step #5

## 0. 架构说明

本文档描述 **Rust `cw_daemon` binary**（对应 [rust_ext/src/bin/cw_daemon.rs](../../rust_ext/src/bin/cw_daemon.rs)）的部署/升级/回滚流程。

### 二进制

- `cw_daemon`：独立 Rust binary（`cargo build --bin cw_daemon --release`），提供 UDS JSON-RPC server
- `cw`：Python CLI，内含 `cw daemon <action>` 子命令作为 RPC 客户端，调用运行中的 `cw_daemon`

### 命令对应关系

| 操作 | 命令 | 说明 |
| --- | --- | --- |
| 启动 daemon | `cw_daemon serve` 或 `systemctl start callwarden-daemon` | Rust binary 直接启动 |
| 健康检查 | `cw_daemon health-check` 或 `cw daemon health` | 前者直接 UDS ping；后者走 Python 客户端 |
| schema 检查 | `cw_daemon schema-check --strict` | systemd `ExecStartPre` 调用 |
| schema 版本 | `cw daemon schema-version` | Python 客户端查询 RPC |
| 备份 registry | `cw daemon backup --output <PATH>` | Python 客户端调用 `backup` RPC |
| 恢复 registry | `cw daemon restore --from <PATH>` | Python 客户端调用 `restore` RPC |
| CAS GC | `cw daemon gc-cas --grace-days N <ws_id>` | Python 客户端调用 `gc.cas` RPC |
| Snapshot GC | `cw daemon gc-snapshots --keep-last N` | Python 客户端调用 `gc.snapshots` RPC |
| drain 排空 | `sudo systemctl kill --signal=SIGUSR1 callwarden-daemon` | SIGUSR1 信号触发 |
| reload 配置 | `sudo systemctl reload callwarden-daemon` | SIGHUP 信号触发 |

### sd_notify 集成

systemd unit 使用 `Type=notify`，`cw_daemon` 启动后通过 `NOTIFY_SOCKET` 发送：
- `READY=1`：server 启动并注册信号后
- `STOPPING=1`：收到 SIGTERM 后，graceful shutdown 前

调试时可加 `--no-sd-notify` 禁用，避免 systemd 误判 READY 状态。

## 1. 首次安装

```bash
# 1. 构建 cw_daemon binary（Linux 主机或交叉编译）
cd rust_ext
cargo build --release --bin cw_daemon
# 产物：target/release/cw_daemon

# 2. 创建 callwarden 用户和组
sudo useradd -r -s /usr/sbin/nologin callwarden
# 可选：创建 callwarden-clients 组，让其他用户通过 UDS 访问 daemon
sudo groupadd -r callwarden-clients
sudo usermod -aG callwarden-clients callwarden

# 3. 安装二进制和 systemd unit
sudo cp target/release/cw_daemon /usr/local/bin/cw_daemon
sudo chmod 0755 /usr/local/bin/cw_daemon
sudo cp cicd/callwarden-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload

# 4. 初始化数据目录（systemd RuntimeDirectory / StateDirectory 会自动创建）
# 手动确认：
sudo mkdir -p /var/lib/callwarden /var/log/callwarden
sudo chown callwarden:callwarden /var/lib/callwarden /var/log/callwarden

# 5. 启动并验证
sudo systemctl enable --now callwarden-daemon
systemctl status callwarden-daemon
# 期望：Active: active (running)，Type=notify 会在 READY=1 后变绿

# 6. 健康检查
cw_daemon health-check
# 或：cw daemon health
```

## 2. 升级（N-1 兼容）

```bash
# 1. 检查当前版本
cw_daemon --version
cw daemon schema-version

# 2. 停止接受新 refresh（graceful drain，SIGUSR1）
sudo systemctl kill --signal=SIGUSR1 callwarden-daemon

# 3. 在线备份（daemon 仍在运行，VACUUM INTO 不阻塞）
cw daemon backup --output /var/backups/callwarden/$(date +%Y%m%d).db

# 4. 停止服务
sudo systemctl stop callwarden-daemon

# 5. 替换二进制
sudo cp target/release/cw_daemon /usr/local/bin/cw_daemon.new
sudo mv /usr/local/bin/cw_daemon /usr/local/bin/cw_daemon.bak
sudo mv /usr/local/bin/cw_daemon.new /usr/local/bin/cw_daemon

# 6. 启动（ExecStartPre 会先做 schema-check --strict，失败则不启动）
sudo systemctl start callwarden-daemon

# 7. 验证
cw_daemon health-check
cw daemon schema-version  # 确认新版本
journalctl -u callwarden-daemon --since "5 min ago" | grep -E "schema|ready|recovered"
```

## 3. 回滚

```bash
# 1. 停止当前版本
sudo systemctl stop callwarden-daemon

# 2. 恢复旧二进制
sudo cp /usr/local/bin/cw_daemon.bak /usr/local/bin/cw_daemon

# 3. 恢复备份数据（registry DB）
cw daemon restore --from /var/backups/callwarden/20260714.db
# 注意：restore 命令需要 daemon 在运行，因此先启动旧版本再 restore

# 4. 启动
sudo systemctl start callwarden-daemon

# 5. 验证
cw_daemon health-check
cw daemon schema-version  # 确认旧版本
```

## 4. 故障恢复

### 4.1 daemon kill -9 后恢复

```bash
# 1. systemd 自动重启（Restart=on-failure）
# 2. 检查 recovery 日志
journalctl -u callwarden-daemon --since "5 min ago" | grep -E "recovered|recovery|staging"

# 3. 验证 staging entries 已恢复（daemon 启动时会自动 recover_all_workspaces）
# 启动日志中应有 "recovered N pending entries from workspaces"
journalctl -u callwarden-daemon --since "5 min ago" | grep "recovered"

# 4. 验证 snapshot generation 单调性
cw daemon status <workspace_instance_id>
# 返回 JSON 中 generation 字段应大于等于崩溃前
```

### 4.2 DB 损坏恢复

```bash
# 1. 停止服务
sudo systemctl stop callwarden-daemon

# 2. 检查 WAL 完整性
sudo -u callwarden sqlite3 /var/lib/callwarden/registry.db "PRAGMA integrity_check;"

# 3. 从备份恢复
sudo systemctl start callwarden-daemon  # 先启动 daemon
cw daemon restore --from /var/backups/callwarden/latest.db

# 4. snapshot 重建：daemon 启动后 SnapshotCache 为空，下次 workspace.connect
#    + snapshot.publish 时会自动重建（按需）
#    如需强制重建所有 workspace 的 snapshot，重启 daemon 即可
sudo systemctl restart callwarden-daemon
```

## 5. GC 手动触发

```bash
# CAS GC（保留 7 天 grace period，需指定 workspace_instance_id）
cw daemon gc-cas --grace-days 7 <workspace_instance_id>

# Snapshot GC（保留每个 workspace 最近 3 个 generation）
cw daemon gc-snapshots --keep-last 3

# Staging log compact：通过 SIGUSR1 信号触发 drain
# daemon 会扫描所有 workspace 的 staging.log，将 applied entries 写入
# 对应的 SQLite DB 并从 staging.log 中删除
sudo systemctl kill --signal=SIGUSR1 callwarden-daemon
journalctl -u callwarden-daemon --since "1 min ago" | grep "drain complete"
```

## 6. 监控与告警

```bash
# 健康状态（通过 UDS JSON-RPC）
cw daemon health
# 返回：{"status":"ok","pid":...,"uptime_seconds":...,"workspace_count":...,
#        "snapshot_workspace_count":...,"schema_version":...}

# 关键日志（journalctl）
journalctl -u callwarden-daemon -f | grep -E "ERROR|WARN|recovered|drain"

# 关键告警规则（基于 journalctl 或 Prometheus exporter）
# - systemd unit 失败 → systemctl status 显示 failed
# - "database is locked" 持续出现 → 检查是否有 CLI 写操作与 MCP 长连接冲突
# - "schema-check" 失败 → ExecStartPre 阻止启动，检查版本兼容性
# - sd_notify READY=1 未发送 → Type=notify 超时（TimeoutStartSec=30）
```

## 7. systemd unit 关键字段说明

```ini
[Service]
Type=notify                    # 依赖 sd_notify READY=1
ExecStartPre=/usr/bin/cw_daemon schema-check --strict
ExecStart=/usr/bin/cw_daemon serve
ExecReload=/bin/kill -HUP $MAINPID    # SIGHUP 触发 reload config
KillMode=mixed                # SIGTERM 发给主进程，子进程也收到
KillSignal=SIGTERM            # 触发 sd_notify STOPPING=1 + graceful drain
TimeoutStopSec=60             # 给 drain 60 秒时间
UMask=0007                    # UDS socket 0660，callwarden-clients 组可访问
RuntimeDirectory=callwarden   # /run/callwarden 由 systemd 创建
StateDirectory=callwarden     # /var/lib/callwarden 由 systemd 创建
```

## 8. 常见问题

### 8.1 Type=notify 启动超时

**现象**：`systemctl status` 显示 `start-pre` 或 `start` 超时。

**排查**：
1. 检查 `journalctl -u callwarden-daemon` 是否有 `[ERROR]` 日志
2. `cw_daemon schema-check --strict` 单独运行看是否 exit 0
3. UDS socket 路径 `/run/callwarden/callwarden.sock` 父目录是否存在

**临时绕过**：使用 `--no-sd-notify` 启动，并将 `Type=notify` 改为 `Type=simple`：
```bash
# 临时调试（不修改 unit 文件）
sudo NOTIFY_SOCKET= /usr/local/bin/cw_daemon serve --no-sd-notify
```

### 8.2 database is locked

**现象**：CLI 命令报 `database is locked`（exit code 2）。

**原因**：MCP Server 长连接持有写锁 + CLI 新进程同时写入。

**解决**：等待几秒重试；持续出现则检查是否有遗留进程：
```bash
sudo fuser /var/lib/callwarden/registry.db
sudo lsof /var/lib/callwarden/registry.db
```

### 8.3 UDS socket permission denied

**现象**：非 callwarden 用户调用 `cw daemon health` 报 permission denied。

**解决**：将用户加入 `callwarden-clients` 组（与 unit 文件 UMask=0007 配合）：
```bash
sudo usermod -aG callwarden-clients <user>
# 重新登录后生效
```
