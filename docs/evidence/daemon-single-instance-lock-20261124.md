# Evidence manifest: daemon 单实例锁现场发现 + host 进程注册表设计（2026-11-24）

- 关联任务：T-1795507699058-41d9fcbc（[ROOT FIX] D3 自愈单实例守卫）
- 关联证据正文：`docs/reports/daemon-single-instance-lock-forensics-20261124.md`
- 用途：①现场发现走 daemon 证据通道；②Task Contract revision 2 的治理证据锚点（只补 host 进程注册表设计 + reviewer 验收栏，D1/D2/D3 主规格不变）。

## 摘要

daemon 为单进程多线程（tokio 2 workers + 独立 HTTP 线程）。权威 manifest（`~/.callwarden/http-daemon.S-1-5-21-….manifest.json`）已含 `pid=2408` / `endpoint=https://127.0.0.1:4523` / `process_start_time` / `daemon_binary_sha256` / `manifest_hash`，即用户要求的「pid + http port 声明」已存在但未被用于单实例守卫。

现有 `DaemonMutex` 三个错：①按 endpoint 派生 lock_id（`daemon_mutex.py:59-66`），4523 与 5854 是两把锁；②只在启动尝试期持锁（`daemon_autostart.py:160-167`），daemon 存活期间不持锁；③1s TCP 探活失败即裸 Popen（`:305-306`），拥塞期误判为无实例。

设计判定：采纳「host 级锁 + manifest pid/start_time 判活 + 原子临界区」；webui（8765）独立注册为 host 进程注册表条目，不与 daemon 锁耦合；无人值守下「问进程在干啥」改为 `E_DAEMON_ALREADY_RUNNING` + 显式 `cw daemon restart --force`。

## 采集方式

源码只读定位（server.rs:48-49/90-91、http_server.rs:981、daemon_mutex.py:59-66/185-187/241、daemon_autostart.py:160-167/305-306、mcp_server.py:74、web_gateway.py:59）+ 本机 manifest 只读 JSON 解析 + registry.db 只读查询；未修改任何 DB / 进程 / 生产文件。
