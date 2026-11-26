# supersede 证据清单：daemon 构建锁阻塞 + MCP 自愈孤儿进程（2026-11-24）

## 1. 观测面全红故障（用户截图，15:29）

- `GET /health` 直连 daemon 4523：**20ms**（2 次采样 29.9ms / 17.2ms）——HTTP 监听无故障。
- 同一时刻浏览器看板：7 个 section + workspace 解析全部 `E_HTTP_DAEMON_UNAVAILABLE / E_HTTP_REQUEST_TIMEOUT`，超时阈值 `HTTP_DEFAULT_TIMEOUT=30.0`（config.py:1688）。
- 结论：**不碰 workspace DB 的端点秒回、碰 workspace DB 的端点全超时** = workspace 锁阻塞特征。

## 2. 锁机制源码自证

- `config.py:1691-1694` 注释原文：「build 期间持有 workspace 锁，会把后续涉及该 workspace 的 RPC 全部拖入超时」；全量索引实测 129.6s（小仓 144 文件）。
- 实测：`cw dashboard` 全量 **29.9s**（exit 2）；`cw dashboard --quick` **0.77s**；网关 `DASHBOARD_DEFAULTS`（web_gateway.py:62-67）已用 quick=True，非网关问题。
- 阵发性：本日 3 次「僵住→自愈」；CLI 一次 17.8s 纯等待（user+sys 0.3s）；恢复后 `task show` 连续三次 1.22/0.96/0.93s。
- `callwarden.db` = 1,630,371,840 bytes（1.63GB），`journal_mode=wal`，WAL 曾 26.8MB 活跃写入；主库规模决定全量扫表代价。

## 3. 孤儿 daemon 机制（用户确认后已清除 PID 6608）

- 进程现场：PID 6608，`cw-daemon.exe`，created 2026-11-24 07:24:18，命令行**无任何参数**，监听 127.0.0.1:5854，父进程已退出；核验身份后终止，端口释放。权威实例 PID 2408 自 2026-11-23 23:37 起带 `--http-bind 127.0.0.1:4523`，CPU 累计 1198s。
- 触发链（源码级）：
  1. `server/mcp_server.py:74` — `HttpDaemonRpcClient(timeout=1.0, verify_health=False, validate_manifest=False)` 探活；阵发拥塞期 1s 内失败即判 daemon 不可达。
  2. `server/mcp_server.py:80-86` — 探活失败 → `spawn_http_daemon(wait_window=15.0)`。
  3. `server/daemon_autostart.py:117-119` — 连接失败即 `_start_daemon_platform(endpoint)`，仅一次。
  4. `server/daemon_autostart.py:305-306` — Windows 分支 `subprocess.Popen([daemon_bin], ...)`：**裸启动，无 endpoint 参数**，daemon 自绑默认端口 5854。
  5. `server/daemon_mutex.py:241` — `DaemonMutex(endpoint)` 按 endpoint 加锁；4523 与 5854 是两把不同的锁，单实例守卫永不冲突。
  6. `server/daemon_autostart.py:300-302` — 注入 `CW_DAEMON_TASK_DB=DB_PATH`（callwarden.db）；第二个 daemon 与权威实例**共写同一 1.63GB 主库**，叠加跨进程 SQLite 写竞争。
- 重复作案：原 backlog 卡 T-1795491554078-3616ae10 描述中已记 2026-11-24 早前派生 workspace 级 daemon PID 44076。
- 三处设计缺陷：①裸 Popen 无权威 endpoint；②daemon 无参可启动（应 fail-closed）；③1s 探活失败即拉起（阵发期连环拉起）。

## 4. SQLite 读写分离可行性

- `PRAGMA journal_mode = wal` 已开启 —— SQLite 原生「单写者 + 多读者快照」能力可用，**不需要换数据库**。
- 阻塞来自应用层单连接串行：`cas.rs:236`、`http_server.rs:244`、`replicator.rs:1224`、`toolchain.rs:121`、`workspace.rs:558` 均为 `Mutex<Connection>`；唯一连接被长写事务占用时，读写全部在同一把 mutex 排队。
- 方案：1 写连接 + N 读连接池（WAL 快照读），build_graph 分批小事务提交，workspace 锁只护写路径；注意 WAL checkpoint 饥饿与 read-after-write 一致性。

## 5. supersede 决策

- 前身：T-1795491554078-3616ae10（[BACKLOG] handle_build_graph_async，open，3 步 pending，2026-11-24 11:39 建卡）。
- 合并原因：原卡白名单只含 build_graph/refresh 路由、build_flight、health/watchdog、cli refresh、tests；D2 连接层与 D3 autostart 均在其外，需正式扩权，且 D1/D2 动同一批文件不宜拆卡。
- 新卡范围：D1 构建隔离 + D2 读写分离 + D3 自愈守卫；原卡 3 步迁移；所有权白名单扩展至 DB 连接层与 server/daemon_autostart.py、daemon_mutex.py、mcp_server.py。
- 不重叠项：T-1795466935294-3433ca6c / T-1795451985855-842b3b64 管审计回执，不在本卡范围。

证据采集人：本执行会话；时间：2026-11-24 15:29–16:05 GMT+8。
