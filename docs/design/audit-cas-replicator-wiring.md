# CAS/Replicator/StagingLog/SnapshotManager 审计报告

> 审计日期：2026-07-13
> 任务：`T-1783952125417-7a09` Step #0
> 审计范围：daemon refresh 管道接通 CAS/Replicator/SnapshotManager 的现有实现缺口

## 1. 组件现状

### 1.1 CAS（db/db_cas.py）—— 生产级

四阶段原子发布（building → contents → symbols/calls → ready）、flock 协调 GC、两阶段
file_generations（seen/committed）、mark-sweep GC 均已实现。

**缺口**：`file_generations` DDL 同时在 `db_cas.py:140-149` 和 `replicator.py:52-62`
定义（SESSION_SCHEMA_DDL），两份 DDL 相同但独立维护。

### 1.2 StagingLog（server/staging_log.py）—— 功能完整，百万级有瓶颈

JSON Lines append-only 日志，append 时 fsync，`mark_applied` / `mark_failed` 会重写
整个文件。设计文档 §4.2 要求"staging log 的状态更新不能每条重写整个 JSONL 文件；
实施 agent 应评估 SQLite WAL log 或 append-only status record"。

**决策**：当前 JSONL 在千级规模可接受。本次实施保留 JSONL 作为 durable append，
新增 `mark_applied_batch(lsns)` 批量标记减少重写次数。百万级优化留给后续任务。

### 1.3 Replicator（server/replicator.py）—— 核心管道已实现

`daemon_handle_connect` 和 `daemon_handle_refresh` 完整实现了 session epoch 分配、
stale session 拒绝、两阶段 CAS（seen/committed）、daemon 侧 re-canonicalize +
re-hash + Rust parse + CAS publish。

**缺口**：
1. **TOCTOU 违规**：`_daemon_parse_and_publish` 先 canonicalize 文件计算 hash，
   再调 `parse_file_lang(abs_path, "")` 让 Rust 重新读文件。canonical bytes 和
   parse 读取的不是同一份数据，违反 parse-input-abi.md §2。
2. **违反禁止读客户端路径**：`daemon_handle_refresh` 用 `workspace_root + rel_path`
   拼接 abs_path 后读取文件，违反 §2.2 禁止按客户端绝对路径读取。
3. **Rust parse_canonical_bytes 未暴露**：`GenericParser::parse_canonical_bytes`
   存在于 `multi_lang.rs:625` 但无 `#[pyfunction]` 包装。
4. **未接入 dispatch**：`daemon_server.py` 的 `workspace.refresh` 走全量 checkpoint
   路径，未调用 `daemon_handle_refresh`。

### 1.4 SnapshotManager（server/snapshot_manager.py）—— 完整

Rust ArcSwap 支持的 GraphSnapshot 查询层，多 workspace 缓存，QueryBudget 控制。
`publish_snapshot` 从 SQLite 加载全量 GraphStore，增量路径留给后续。

### 1.5 IPC 双协议（daemon_protocol.py + ipc_transport.py）

`daemon_protocol.py` 的 `recv_message_with_fds` 是首包 recvmsg 同时接 framing header
和 ancillary FD 的正确实现。`ipc_transport.py` 的 `_recv_msg_with_fd` 先 recv
header 再 recvmsg FD，可能丢失 ancillary data。设计文档 §2.3 要求统一到
`daemon_protocol.py` 路径。

**决策**：本次将 `ipc_transport.py` 标记为 deprecated，新增的 refresh 管道只使用
`daemon_protocol.py`。

## 2. 事务边界（daemon 单写者不变量）

```
workspace registry DB  ─── daemon 写（register/status update）
CAS DB                 ─── daemon 写（cas_publish/cas_pin/cas_gc）
workspace ws_conn      ─── daemon 写（session/file_generations）
staging log            ─── daemon 写（append/mark_applied）
snapshot               ─── daemon 写（ArcSwap publish）
```

agent/client 只能：
- 通过 UDS 发送 canonical bytes 或 FD
- 报告 session_id/seq/epoch
- 查询 snapshot（只读 GraphStore）

daemon 不信任：
- agent 提交的 UID（取 SO_PEERCRED）
- agent 提交的 hash（daemon 重新计算）
- agent 提交的 abs_path（daemon 从 bytes/FD/Git mirror 获取内容）

## 3. 提交顺序（8 步原子链，设计文档 §4.3）

| 步骤 | 操作 | 崩溃恢复策略 |
|------|------|-------------|
| 1 | generation seen CAS | 无状态，下次 refresh 重入 |
| 2 | canonicalize/hash/parse | 纯计算，重做 |
| 3 | CAS publish/pin | CAS 'building' 由 GC 清理 |
| 4 | staging append + fsync | pending entry 被 recover 重放 |
| 5 | workspace manifest/projection 条件提交 | CAS committed 条件 UPDATE |
| 6 | generation committed CAS | 条件 UPDATE 失败 = stale |
| 7 | SnapshotManager 原子发布 | recover 重新发布 |
| 8 | staging applied/compact | 幂等 |

## 4. 实施计划

1. **Rust 暴露 `parse_canonical_bytes_py`**：从 Python 传入 canonical bytes +
   module_path + language，消除 TOCTOU。
2. **改造 `daemon_handle_refresh`**：接收 canonical_bytes（从 UDS bytes frame 或
   FD 读取），不再拼接 abs_path。
3. **dispatch 改造**：新增 `workspace.connect` / `workspace.recover` RPC；
   `workspace.refresh` 走 daemon_handle_refresh 路径。
4. **EnterpriseDaemonService 初始化**：按 workspace 创建 CAS conn、StagingLog、
   Replicator。
5. **集成测试**：CAS hit/miss、stale session、crash recovery、并发 reader。
