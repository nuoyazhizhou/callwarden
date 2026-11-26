# 现场取证：daemon 单实例锁与 host 进程注册表需求（2026-11-24 17:00）

- 关联任务：T-1795507699058-41d9fcbc（[ROOT FIX] D3 自愈单实例守卫）
- 触发：用户提出「cw-daemon 不应重复启动；应声明 pid / http port / webui port，发现端口上无实例才启动」
- 采集方式：源码只读（`Grep`/`Read`）+ 本机 manifest 只读解析（Python）+ 进程/端口只读查询；未修改任何 DB / 进程 / 生产文件。
- 归属：主规格按现有 D3 描述执行；本文为现场发现证据，不改 D1/D2/D3 主范围。host 进程注册表设计单独补入 Task Contract revision 2 + reviewer 验收栏。

## 一、进程模型事实

| 项 | 证据 | 结论 |
|---|---|---|
| daemon 进程模型 | `rust_ext/src/daemon/server.rs:48-49`、`:90-91`：`tokio::runtime::Builder::new_multi_thread().worker_threads(2)`；HTTP accept 在独立 `cw-http-mvp` 线程（`http_server.rs:981` 同配置） | **单进程多线程**，非单进程单线程 |
| 进程内串行点 | `Arc<TokioMutex<S>>` + `SerializationPoint` | 进程内已串行；问题在**进程间** |
| manifest 内容 | 本机 `~/.callwarden/http-daemon.S-1-5-21-….manifest.json` 实测：`pid=2408`、`endpoint=https://127.0.0.1:4523`、`process_start_time=1795491435`、`daemon_binary_sha256=a77f…`、`authority_id=S-1-5-21-…`、`manifest_hash=ea9a…`、`schema=callwarden-http-manifest/v2` | **用户要求的 pid + http port 声明已存在**，且带 `process_start_time` 可防 PID 复用 |
| registry 进程登记 | `registry.db daemon_state` 仅 `schema_version=60`；无 pid/port 字段 | 权威 DB 不承担进程登记 |
| webui port | `server/web_gateway.py:59` `DEFAULT_PORT=8765`；独立 Python 进程，与 daemon manifest 无关 | daemon 不拥有 webui 端口，硬塞进 daemon 锁文件会造成跨进程耦合 |
| 现存 pid 文件 | `runtime/bridge.pid=65352`、`runtime/evidence/cw-daemon.persist.pid=9236` | 只有 bridge/daemon 的孤立 pid 文件，无统一 host 级注册表 |

## 二、现有互斥为什么没挡住（三个错）

| # | 错在哪 | 代码证据 | 后果 |
|---|---|---|---|
| 1 | **锁粒度错**：按 endpoint 派生 lock_id | `server/daemon_mutex.py:59-66` `sha256(endpoint)[:16]`；`:241` `DaemonMutex(endpoint)` | 权威 `4523` 与孤儿默认 `5854` 是两把锁，永不冲突 |
| 2 | **锁生命周期错**：只在启动尝试期持有 | `server/daemon_autostart.py:160-167` `try_acquire()` → `finally: release()` | daemon 存活期间不持锁，后到会话仍能再抢锁启动 |
| 3 | **触发策略错**：1s TCP 探活失败即拉起 | `server/mcp_server.py:74` `timeout=1.0` → `daemon_autostart.py:305-306` `subprocess.Popen([daemon_bin])` 裸启动 | 全量索引拥塞期探活超时被误判为「无实例」，拉起第二进程 |

Unix 实现虽写 pid 到锁文件（`daemon_mutex.py:185-187`），但仅调试用途，无判活逻辑；Windows 命名互斥体 `Global\CallWarden_Daemon_<lock_id>` 同理只按 endpoint。

## 三、用户方案评估

| 设计点 | 判定 | 理由 |
|---|---|---|
| 锁 host 级实例、不按 endpoint | ✓ 采纳 | 直接对应错误 1，根修卡 D3 已写「按 authority/task_db 加锁」 |
| 声明 pid + port、判活后再启动 | ✓ 采纳（复用 manifest，不新建重复数据） | manifest 已含 pid/endpoint/process_start_time，比裸 pid 更严谨 |
| 纯「读锁文件→判断→改文件→启动」 | ⚠️ 必须加 OS 原子锁 | 两 MCP 宿主同时读、同时判断、同时启动 = TOCTOU；用现有 flock / Windows named mutex 原语守住整个临界区 |
| 「问进程在干啥，决定要不要重启」 | ⚠️ 改为结构化错误 + 显式 CLI | MCP 宿主无人值守，不能弹窗；返回 `E_DAEMON_ALREADY_RUNNING`（含 pid/endpoint/manifest/作业状态），重启交给 `cw daemon restart --force` 人工决定 |
| webui port 写进同一把 daemon 锁 | ⚠️ 改为 host 进程注册表独立条目 | webui 是独立进程，daemon 不拥有它；用 `authority_id` 关联 bridge / daemon / webui 三类 entry，避免跨进程生命周期耦合 |

## 四、host 进程注册表设计（补入 D3 合同 r2）

- 位置：与 authority manifest 同根的 `~/.callwarden/runtime/host-process-registry.json`；按 `authority_id` 隔离。
- schema：`callwarden-host-process-registry/v1`；`entries[]` 每条形如 `{role, pid, process_start_time, endpoint_or_bind, binary_sha256, started_at, status}`；`role ∈ {bridge, daemon, webui}`。
- 发布：沿用 `publish_manifest_atomic` 的 temp+fsync+rename 原子写法；每个进程启动时 upsert 自身 entry，正常退出删除自身 entry；崩溃残留 entry 由权威 daemon 用 `pid + process_start_time` 判活后标 stale（不直接删历史）。
- 判活：单实例守卫读注册表 + daemon manifest；`pid` 存活且 `process_start_time` 匹配才视作活实例；PID 复用导致 start_time 不符 → 视为 stale，允许启动。
- webui 独立注册：`python -m callwarden.server.web_gateway` 启动时登记 `role=webui, bind=127.0.0.1:8765`，退出注销；webui 启停不触发 daemon 单实例守卫，daemon 重启不清理 webui entry。
- 不耦合：manifest 保持 daemon 权威（含 mTLS 细节），注册表只做轻量索引；两者 endpoint/pid 冲突时以 manifest 为准并上报结构化错误。
- 白名单影响：新增 `server/host_process_registry.py`（新模块）、`server/web_gateway.py`、`rust_ext/src/daemon/http_server.rs`（manifest 判活辅助）、`server/daemon_mutex.py`、`server/mcp_server.py`、`tests/test_host_process_registry.py` 到 Task Contract allowed_edit_scope；不改 task 治理表 / schema 治理结构 / role_prompts 资产。

## 五、验收补充（reviewer 栏）

1. 注册表原子发布与 `pid+process_start_time` 判活经独立复审；PID 复用负向测试。
2. webui 独立注册不与 daemon 单实例锁耦合；daemon 重启不误杀 webui entry。
3. 模拟 1s 探活失败而 daemon 存活（如全量索引拥塞期）时，不得拉起第二 daemon，返回 `E_DAEMON_ALREADY_RUNNING` + 结构化诊断。
4. 两份证据 manifest/hash 在本卡 reviewer 记录中可复核：本文 + `docs/evidence/daemon-single-instance-lock-20261124.md`。
