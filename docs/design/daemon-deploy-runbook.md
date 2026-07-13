# Call Warden Enterprise Daemon — 部署/升级/回滚 Runbook

> 任务：T-1783974522652-e0c7 Step #5

## 1. 首次安装

```bash
# 创建 callwarden 用户
sudo useradd -r -s /usr/sbin/nologin callwarden

# 安装二进制和 systemd unit
sudo cp target/release/cw /usr/local/bin/cw
sudo cp cicd/callwarden-daemon.service /etc/systemd/system/
sudo systemctl daemon-reload

# 初始化数据目录
sudo mkdir -p /var/lib/callwarden /var/log/callwarden
sudo chown callwarden:callwarden /var/lib/callwarden /var/log/callwarden

# 启动并验证
sudo systemctl enable --now callwarden-daemon
systemctl status callwarden-daemon
cw daemon health  # 检查 readiness
```

## 2. 升级（N-1 兼容）

```bash
# 1. 检查当前版本和 schema
cw daemon version
cw daemon schema-version

# 2. 停止接受新 refresh（graceful drain）
sudo systemctl kill --signal=SIGUSR1 callwarden-daemon

# 3. 等待队列排空
cw daemon queue-stats  # 等待 pending=0

# 4. 在线备份
cw daemon backup --output /var/backups/callwarden/$(date +%Y%m%d)

# 5. 停止服务
sudo systemctl stop callwarden-daemon

# 6. 替换二进制
sudo cp target/release/cw /usr/local/bin/cw

# 7. 启动（自动迁移 schema）
sudo systemctl start callwarden-daemon

# 8. 验证
cw daemon health
cw daemon schema-version  # 确认新版本
```

## 3. 回滚

```bash
# 1. 停止当前版本
sudo systemctl stop callwarden-daemon

# 2. 恢复旧二进制
sudo cp /usr/local/bin/cw.bak /usr/local/bin/cw

# 3. 恢复备份数据
cw daemon restore --from /var/backups/callwarden/20260714

# 4. 启动
sudo systemctl start callwarden-daemon

# 5. 验证
cw daemon health
cw daemon schema-version  # 确认旧版本
```

## 4. 故障恢复

### 4.1 daemon kill -9 后恢复

```bash
# 1. systemd 自动重启（Restart=on-failure）
# 2. 检查 recovery 日志
journalctl -u callwarden-daemon --since "5 min ago" | grep recovery

# 3. 验证 staging entries 已恢复
cw daemon staging-stats  # pending/applying 应为 0

# 4. 验证 snapshot generation 单调性
cw daemon generation-check
```

### 4.2 DB 损坏恢复

```bash
# 1. 停止服务
sudo systemctl stop callwarden-daemon

# 2. 检查 WAL 完整性
sqlite3 /var/lib/callwarden/cas.db "PRAGMA integrity_check;"

# 3. 从备份恢复
cw daemon restore --from /var/backups/callwarden/latest

# 4. 重建 snapshot
sudo systemctl start callwarden-daemon
cw daemon force-rebuild-snapshots
```

## 5. GC 手动触发

```bash
# CAS GC（保留 7 天 grace period）
cw daemon gc-cas --grace-days 7

# Snapshot GC
cw daemon gc-snapshots --keep-last 3

# Staging log compact
cw daemon staging-compact --keep-last 1000
```

## 6. 监控与告警

```bash
# 指标端点
curl http://localhost:9090/metrics | grep cw_

# 关键告警规则
# - cw_daemon_unhealthy == 1 → P1
# - cw_queue_depth > 10000 持续 5 分钟 → P2
# - cw_cas_gc_failures_total > 0 → P2
# - cw_recovery_duration_seconds > 60 → P3
```
