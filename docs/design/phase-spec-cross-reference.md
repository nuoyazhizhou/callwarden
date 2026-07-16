# Enterprise Daemon 实施阶段 ↔ 短规范跨切面映射

> 本文件记录每个实施 Phase 必须遵循的短规范章节，作为 task step 描述的补充。
> 实施者在 `task next` 拿到 step 后，必须同时阅读对应的短规范章节。

## Phase 2: Daemon Skeleton + UDS + Workspace Registry

| Step | 必须遵循的短规范 | 章节 |
|------|----------------|------|
| #0 Rust daemon crate | daemon-ipc-security.md §1 通信架构 | 安全边界定义 |
| #1 UDS server | daemon-ipc-security.md §2.1 UDS 路径与权限 | 0660 权限 + 属主 |
| #2 SO_PEERCRED | daemon-ipc-security.md §5 Agent 不可信约束 | 哪些值不信任 |
| #5 register/list/status API | watcher-generation-state-machine.md §4.1 daemon_handle_connect | 握手分配 epoch + revoke 旧 session |
| #6 Python CLI daemon client | daemon-ipc-security.md §2.2 长度分帧 UDS stream | 消息头格式 |

## Phase 3: Global CAS + Workspace Manifest

| Step | 必须遵循的短规范 | 章节 |
|------|----------------|------|
| #0 CAS schema | cas-gc-protocol.md §2 CAS Schema | 完整建表语句 + 自包含约束 |
| #1 CAS key | cas-gc-protocol.md §1 CAS Key 设计 | compute_cas_key_v1 唯一入口 |
| #2-#3 daemon refresh + parse worker | cas-gc-protocol.md §3 CAS 原子发布协议 | 四阶段 building→ready + TOCTOU 修复 |
| #4 clean snapshot manifest | cas-gc-protocol.md §4 file_generations 两阶段 CAS | seen + committed 两阶段 |
| #5 dirty overlay manifest | parse-input-abi.md §2 canonicalize_source | content_hash 基于 canonical bytes |
| #6 CAS GC | cas-gc-protocol.md §5 唯一 GC 协议 | LOCK_EX→BEGIN→scan→sweep→COMMIT + 删除顺序 |

## Phase 4: Snapshot Query Service

| Step | 必须遵循的短规范 | 章节 |
|------|----------------|------|
| 全部 | parse-input-abi.md §3 不变量 | content_hash 基于 canonical bytes（I12）|
| 新增: diff_callers/diff_callees | cas-gc-protocol.md §4 | resolved edge delta 基于 workspace_manifests |
| 新增: compare_snapshots | cas-gc-protocol.md §4 | generation 比较基于 latest_committed_generation |

## Phase 5: 秒级 Watcher + Delta Replicator

| Step | 必须遵循的短规范 | 章节 |
|------|----------------|------|
| #0 Rust notify crate | daemon-ipc-security.md §1 | agent 侧 canonicalize_source（Rust FFI）|
| #0-#2 文件变更检测 | parse-input-abi.md §2 canonicalize_source | 编码检测 + CRLF 归一化 + content_hash |
| #3 parse delta / resolve delta | parse-input-abi.md §3 不变量 I1-I12 | CRLF/emoji/GBK/UTF-16 偏移映射 |
| #6 Staging durable log | watcher-generation-state-machine.md §4 状态机 | session epoch + 两阶段 CAS |
| #7 Replicator | watcher-generation-state-machine.md §4.3 daemon_handle_refresh | generation 去重 + manifest 条件提交 |
| #7 Replicator | daemon-ipc-security.md §3 memfd 密封协议 | 大文件 > 16MB 走 memfd + 四重 seal |
| #7 Replicator | daemon-ipc-security.md §4 Inflight Bytes 限制 | 背压：per-conn 256MB / daemon 2GB / per-UID 512MB |

## Phase 6: Toolchain CAS

无短规范跨切面（toolchain_fingerprint 在主设计 enterprise-daemon-shared-snapshot-plan.md §9 定义）。

## Phase 7: Heavy Jobs

无短规范跨切面。

## Phase 8: 生产化

| Step | 必须遵循的短规范 | 章节 |
|------|----------------|------|
| #0 systemd unit | daemon-ipc-security.md §2.1 | UDS 权限 0660 + 属主 callwarden:callwarden |
| #4 audit log | watcher-generation-state-machine.md §2 | agent_sessions 表记录 session epoch 生命周期 |
| #7 snapshot GC | cas-gc-protocol.md §5 唯一 GC 协议 | 与 Phase 3 同一 GC 实现 |
| #8 chaos tests | watcher-generation-state-machine.md §8 故障注入测试 | S1-S2 barrier 竞态测试 |
| #8 chaos tests | daemon-ipc-security.md §7 故障注入测试 | memfd seal 缺失/篡改/超限 |
