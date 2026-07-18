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

## 9. cw-agent 部署（G9: per-UID systemd --user watcher）

> 任务：T-1784374569181-210c540f（G9 Per-UID systemd --user agent）
> 设计参考：[enterprise-architecture-evolution.md](enterprise-architecture-evolution.md) §v8

### 9.1 架构说明

**cw-agent** 是 per-UID 的文件监控 agent，作为 systemd --user 实例运行：

- 监控用户工作目录（默认 `$HOME/work`）的文件变更
- 通过 Rust `canonicalize_source_py` 生成规范化字节流
- 通过 UDS 发送 `workspace.file.refresh` RPC 到 system daemon
- 不直接访问数据库，所有写入通过 daemon RPC

**关键文件**：
- 入口脚本：`cw-agent`（Python，调用 `callwarden.cli.main:run_agent_mode`）
- 核心模块：[server/agent_session.py](../../server/agent_session.py) / [server/agent_protocol.py](../../server/agent_protocol.py) / [server/agent_watcher.py](../../server/agent_watcher.py)
- systemd unit：[release/linux/deb/systemd/callwarden-agent.service](../../release/linux/deb/systemd/callwarden-agent.service)
- PID 文件：`~/.callwarden/agent.pid`
- Session 文件：`~/.callwarden/agent_session.json`（持久化 session_id）
- 日志文件：`~/.callwarden/agent.log`（systemd 自动重定向到 journal）

### 9.2 首次安装

#### 9.2.1 通过 deb 包安装（推荐）

```bash
# 1. 安装 callwarden-agent 包（自动安装 cw-agent binary + systemd user unit）
sudo dpkg -i callwarden-agent_<version>_<arch>.deb

# 2. （管理员）为需要 agent 的用户启用 linger（允许用户 service 在未登录时运行）
sudo loginctl enable-linger <user>

# 3. （用户）启动并启用 agent
systemctl --user daemon-reload
systemctl --user enable --now callwarden-agent.service

# 4. 验证
systemctl --user status callwarden-agent.service
cw-agent status
```

#### 9.2.2 手动安装

```bash
# 1. 安装 cw-agent binary
sudo cp dist/linux/cw-agent /usr/bin/cw-agent
sudo chmod 0755 /usr/bin/cw-agent

# 2. 安装 systemd --user unit
sudo mkdir -p /usr/lib/systemd/user
sudo cp release/linux/deb/systemd/callwarden-agent.service /usr/lib/systemd/user/

# 3. 用户启用
systemctl --user daemon-reload
systemctl --user enable --now callwarden-agent.service
```

### 9.3 配置

#### 9.3.1 监控目录

默认监控 `$HOME/work`。可通过环境变量或 systemctl edit 覆盖：

```bash
# 临时覆盖（重启失效）
systemctl --user edit callwarden-agent.service
```

在编辑器中输入：
```ini
[Service]
Environment=CW_AGENT_WATCH_DIR=/path/to/your/project
```

或直接修改 unit 文件中的 `Environment=CW_AGENT_WATCH_DIR=%h/work` 行。

#### 9.3.2 daemon socket 路径

默认 `/run/callwarden/callwarden.sock`。可通过 `CW_DAEMON_SOCKET` 环境变量覆盖：

```ini
[Service]
Environment=CW_DAEMON_SOCKET=/custom/path/callwarden.sock
```

#### 9.3.3 日志查看

```bash
# systemd journal（推荐）
journalctl --user -u callwarden-agent.service -f

# 或查看文件日志
tail -f ~/.callwarden/agent.log
```

### 9.4 启动流程

`cw-agent start` 命令的执行顺序：

1. **加载 AgentSession**：从 `~/.callwarden/agent_session.json` 加载（或创建新）session_id
2. **推导 workspace_instance_id**：从 `--watch-dir` 路径的 SHA-256 前 16 位推导
3. **写 PID 文件**：`~/.callwarden/agent.pid`（用于 stop/status）
4. **ping daemon**：验证 daemon UDS socket 可达
5. **握手**：调用 `workspace.connect` RPC，daemon 分配 `session_epoch`
6. **注册 workspace**：调用 `workspace.register` RPC
7. **启动 watchdog Observer**：监控 `--watch-dir` 下的文件变更
8. **阻塞等待 stop_event**：systemd SIGTERM 信号触发退出

### 9.5 文件变更处理流程

watchdog 检测到文件变更后，触发 `user_agent_handle_refresh()`：

1. **canonicalize_source_py**（Rust 扩展）：
   - 读取文件内容
   - BOM/换行/编码归一化
   - 计算 sha256（canonical bytes）
2. **计算 rel_path**：相对 watch_dir 的路径
3. **分配 monotonic_seq**：AgentSession.next_seq()（单调递增）
4. **发送 refresh RPC**：调用 `workspace.file.refresh`
   - 小文件（≤16MB）：`canonical_bytes_hex`（JSON 内嵌）
   - 大文件（>16MB）：写临时文件 → `call_with_fd`（FD 传递，Linux only）
5. **daemon 响应**：`{"status": "committed", "generation": "..."}` 或 `{"status": "stale_seq_dropped"}`

### 9.6 停止 / 状态 / 重启

```bash
# 停止
systemctl --user stop callwarden-agent.service
# 或：cw-agent stop（读取 PID 文件，发送 SIGTERM）

# 状态
systemctl --user status callwarden-agent.service
# 或：cw-agent status（显示 PID + session + daemon 连接状态）

# 重启
systemctl --user restart callwarden-agent.service

# 修改配置后 reload systemd
systemctl --user daemon-reload
systemctl --user restart callwarden-agent.service
```

### 9.7 故障恢复

#### 9.7.1 agent crash 后重启

systemd `Restart=on-failure` 会自动重启（5 秒间隔）：

```bash
# 查看重启历史
systemctl --user status callwarden-agent.service
# 应看到 "Restart=on-failure" 和重启计数

# 查看崩溃日志
journalctl --user -u callwarden-agent.service --since "1 hour ago"
```

#### 9.7.2 session_epoch 重新协商

agent 重启后会从 `~/.callwarden/agent_session.json` 加载 session_id，但 **epoch 被清零**（必须重新协商）。
daemon 在新 `workspace.connect` RPC 时会：
1. 撤销旧 active session
2. 分配新 epoch = MAX(all) + 1
3. 重置 `file_generations.latest_seq = 0`

因此 agent 重启不会丢失数据，但 in-flight 的 refresh 消息会被丢弃（stale_seq_dropped）。

#### 9.7.3 daemon 重启后 agent 行为

daemon 重启后：
- daemon 侧的 `agent_sessions` 表数据丢失（除非持久化到磁盘）
- agent 下次 refresh 时会收到 `ProtocolError: no active session for workspace` 或 `stale_seq_dropped`
- agent 应该捕获此错误并重新调用 `workspace.connect` 协商新 epoch

> **TODO**：当前 `agent_watcher.py` 的错误处理仅记录日志，未自动重连。
> 生产环境应在 `handle_file_change` 失败时检查 `ProtocolError.code`，
> 若为 `session_not_active` 则自动调用 `user_agent_connect()` 重新协商。

### 9.8 资源限制

systemd unit 中的资源限制（可在 `systemctl --user edit` 中覆盖）：

```ini
MemoryHigh=256M       # 软限制，超过后 systemd 限制分配
MemoryMax=512M        # 硬限制，超过后 OOM kill
LimitNOFILE=4096      # 文件描述符上限
TasksMax=64           # 进程/线程数上限
```

watchdog Observer 通常占用 ~50MB 内存，文件 canonicalize 是流式的不会大量分配内存。

### 9.9 安全约束

```ini
NoNewPrivileges=yes       # 禁止 setuid 提权
ProtectSystem=strict      # /usr, /boot 只读
ProtectHome=read-only     # /home, /root 只读（除 ReadWritePaths）
PrivateTmp=yes            # 私有 /tmp
ReadWritePaths=%h/.callwarden  # 仅允许写 ~/.callwarden/
```

agent 需要读用户文件（workspace 目录），但只能写 `~/.callwarden/`（session/pid/log/tmp 文件）。

### 9.10 与 daemon 的依赖关系

cw-agent 是 **user service**，cw_daemon 是 **system service**。systemd 不允许 user service 直接 `Requires=` system service。

替代方案：
- agent 启动时 `ping` daemon，失败则退出（exit 2，systemd 自动重启）
- daemon 重启时 agent 收到 RPC 错误，记录日志但继续运行（下次文件变更时重试）
- 监控建议：分别监控 `callwarden-daemon.service`（system）和 `callwarden-agent.service`（user）的状态

### 9.11 多用户场景

每个用户独立运行自己的 `callwarden-agent.service` 实例：

- 用户 A：监控 `/home/alice/work`，session_id=agent-aaa...
- 用户 B：监控 `/home/bob/code`，session_id=agent-bbb...
- 两个 agent 通过同一 UDS socket 连接同一 daemon
- daemon 通过 `peer_uid` 区分不同用户的 workspace

每个用户的 `~/.callwarden/agent_session.json` 和 `~/.callwarden/agent.pid` 完全独立。

### 9.12 常见问题

#### 9.12.1 cw-agent start 失败：daemon 不可达

**现象**：`cw-agent start` 报 `daemon 不可达：daemon_unreachable`。

**排查**：
1. 确认 daemon 已启动：`sudo systemctl status callwarden-daemon.service`
2. 确认 UDS socket 存在：`ls -l /run/callwarden/callwarden.sock`
3. 确认用户在 `callwarden-clients` 组：`id <user>`
4. 测试 ping：`cw-agent status`（status 命令会 ping daemon）

#### 9.12.2 文件变更未触发 refresh

**现象**：修改文件后 daemon 日志没有 refresh 记录。

**排查**：
1. 确认 agent 在运行：`cw-agent status`
2. 确认文件扩展名在支持列表中（查看 agent 日志 "支持的扩展名" 行）
3. 查看 agent 日志：`journalctl --user -u callwarden-agent -f`
4. 确认 watch-dir 路径正确：`cw-agent status` 或 `systemctl --user show callwarden-agent -p Environment`

#### 9.12.3 session_epoch 未协商

**现象**：agent 日志报 `session_not_active` 或 `seq_alloc_failed`。

**原因**：agent 启动时 `workspace.connect` RPC 失败，但 agent 继续运行（bug）。

**解决**：
1. 重启 agent：`systemctl --user restart callwarden-agent`
2. 检查 `~/.callwarden/agent_session.json` 中 epoch 是否为 0
3. 手动 ping daemon 并查看 daemon 日志确认 `workspace.connect` 是否被调用
