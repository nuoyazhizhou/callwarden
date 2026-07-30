# Phase 4-3 契约：metrics、health、audit 与 admin operations

**Task ID**: `T-1785223331281-eb56dcf0`（Phase 4-3）
**状态**: contract
**日期**: 2026-07-28

## 1. 范围

Phase 4-3 迁移 daemon 的运行时 metrics、健康检查、审计日志和 admin operations 到 Rust PyO3 暴露层。

**涉及**：
- 健康检查：`HealthChecker.check_all()`（Rust 已有 `daemon/health.rs` 完整实现）
- metrics 纯计算：`Counter` / `Gauge` / `Histogram` 数值操作 + Prometheus/JSON 格式化
- audit 纯计算：`canonical_json` / `_compute_signature`（HMAC-SHA256/SHA-256）
- backup 纯计算：`_compute_file_sha256` / `_compute_meta_checksum`

**不涉及**（保持 Python，涉及 I/O 或 DB）：
- `get_memory_info()` / `get_cpu_info()`（系统调用：读 `/proc`、psutil、Windows Psapi）
- `AuditLogger` 所有 DB 读写（SQLite 连接管理）
- `BackupManager` / `RestoreManager` 的 SQLite backup API + 文件复制
- `RecoveryHandler` 的所有 `_recover_*` 方法（SQLite 读写 + 文件系统操作）
- `dump_to_file()` / `load_from_file()`（文件 I/O）

## 2. Python 真相源

| Python 文件 | 函数/类 | 迁移方式 | 优先级 |
|---|---|---|---|
| `server/health_check.py:HealthChecker` | `check_all()` 4 项检查 | Rust 短路（已有 `daemon/health.rs`） | P0 |
| `server/metrics.py:Counter/Gauge/Histogram` | 数值操作 + 线程安全 | Rust pyclass | P1 |
| `server/metrics.py:MetricsCollector` | `to_prometheus()` / `to_json()` 格式化 | Rust 纯函数 | P1 |
| `server/metrics.py:_percentile` | 分位数计算 | Rust 纯函数 | P1 |
| `server/metrics.py:_format_labels/_unformat_labels` | Prometheus 标签格式化 | Rust 纯函数 | P1 |
| `db/db_audit_chain.py:canonical_json` | 稳定序列化 | Rust 纯函数 | P2 |
| `db/db_audit_chain.py:_compute_signature` | HMAC-SHA256 / SHA-256 | Rust 纯函数 | P2 |
| `server/audit_log.py:AuditEvent._generate_event_id` | 时间戳 + 随机 hex | Rust 纯函数 | P2 |
| `server/backup_restore.py:_compute_file_sha256` | SHA-256 哈希 | Rust 纯函数 | P3 |
| `server/backup_restore.py:_compute_meta_checksum` | meta JSON 哈希 | Rust 纯函数 | P3 |

## 3. API 契约

### 3.1 健康检查（P0，Rust 已有实现）

#### `health_check_all(registry_db_path: &str, data_root: &str, start_time_secs: f64, memory_max_bytes: u64) -> String`

**行为**：执行 4 项检查（db_registry / disk_space / memory_usage / uptime），返回 JSON 字符串。

**Rust 实现**：`rust_ext/src/daemon/health.rs:HealthChecker::check_all()`（已完整实现）

**返回**：JSON 字符串，格式与 Python `check_all()` 一致：
```json
{
  "status": "healthy|degraded|unhealthy",
  "checks": {
    "db_registry": {"status": "healthy|degraded|unhealthy", "message": "..."},
    "disk_space": {"status": "...", "message": "..."},
    "memory_usage": {"status": "...", "message": "..."},
    "uptime": {"status": "...", "message": "..."}
  },
  "timestamp": "..."
}
```

**Python 真相源**：`server/health_check.py:HealthChecker.check_all()` (L180-230)

**预期差异**：
- 内存检查：Rust 非 Linux 平台返回 "unsupported"（status=healthy），Python 有 psutil + Windows Psapi fallback
- `RecoveryHandler.recover_stale_jobs()`：Rust 是占位实现，Python 会查 `jobs` 表

### 3.2 metrics 纯计算（P1）

#### `metrics_percentile(values: Vec<f64>, percentile: f64) -> f64`

**行为**：计算分位数（线性插值法）。

**Python 真相源**：`server/metrics.py:_percentile()` (L100-120)

#### `metrics_format_labels(labels: Dict<String, String>) -> String`

**行为**：格式化 Prometheus 标签（`{key="value",key2="value2"}`）。

**Python 真相源**：`server/metrics.py:_format_labels()` (L50-65)

### 3.3 audit 纯计算（P2）

#### `audit_canonical_json(payload: &str) -> String`

**行为**：稳定序列化 JSON（key 排序 + 紧凑分隔符）。

**Python 真相源**：`db/db_audit_chain.py:canonical_json()` (L80-95)

#### `audit_compute_signature(prev_signature: &str, payload_hash: &str, hmac_key: Option<&[u8]>) -> String`

**行为**：
- 有 HMAC key：`HMAC-SHA256(key, prev_signature + "|" + payload_hash)` → hex
- 无 HMAC key：`SHA-256(prev_signature + "|" + payload_hash)` → hex

**Python 真相源**：`db/db_audit_chain.py:_compute_signature()` (L100-115)

### 3.4 backup 纯计算（P3）

#### `backup_compute_file_sha256(path: &str) -> String`

**行为**：计算文件 SHA-256 哈希（hex）。

**Python 真相源**：`server/backup_restore.py:_compute_file_sha256()` (L200-215)

#### `backup_compute_meta_checksum(meta: &str) -> String`

**行为**：对 meta JSON（排除 `checksum` 字段）做 SHA-256。

**Python 真相源**：`server/backup_restore.py:_compute_meta_checksum()` (L220-235)

## 4. 行为契约

### D1: health_check_all

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D1.1 | 所有检查通过 | status=healthy |
| D1.2 | 磁盘空间 >=85% | status=degraded |
| D1.3 | 磁盘空间 >=95% | status=unhealthy |
| D1.4 | 内存 >=80% | status=degraded |
| D1.5 | 内存 >=95% | status=unhealthy |
| D1.6 | uptime <5s | status=degraded |
| D1.7 | registry DB 不存在 | db_registry check=unhealthy |

### D2: metrics_percentile

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D2.1 | [1,2,3,4,5], p50 | 3.0 |
| D2.2 | [1,2,3,4,5], p99 | 5.0（或线性插值） |
| D2.3 | 空列表 | 0.0 |
| D2.4 | 单元素 [42], p50 | 42.0 |

### D3: metrics_format_labels

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D3.1 | {"method":"ping"} | `{method="ping"}` |
| D3.2 | {"method":"ping","status":"ok"} | `{method="ping",status="ok"}` |
| D3.3 | {} | `` (空字符串) |

### D4: audit_canonical_json + audit_compute_signature

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D4.1 | canonical_json({"b":2,"a":1}) | `{"a":1,"b":2}` |
| D4.2 | compute_signature("", "hash123", Some(key)) | HMAC-SHA256 hex |
| D4.3 | compute_signature("", "hash123", None) | SHA-256 hex |
| D4.4 | 链式签名（prev + payload） | 与 Python 一致 |

### D5: backup_compute_file_sha256 + backup_compute_meta_checksum

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D5.1 | 已知文件 | SHA-256 hex（64 字符） |
| D5.2 | meta JSON（含 checksum 字段） | 排除 checksum 后的 SHA-256 |

## 5. 预期差异

1. **health 内存检查跨平台**：Rust `health.rs` 非 Linux 平台返回 "unsupported"（status=healthy），Python `health_check.py` 有 psutil + Windows Psapi fallback。wire-production 层需保留 Python fallback 以支持 Windows 内存检查。

2. **health RecoveryHandler**：Rust `recover_stale_jobs()` 是占位实现，Python 会查 `jobs` 表。wire-production 层不接入 RecoveryHandler（保持 Python）。

3. **metrics Counter/Gauge/Histogram**：Rust 用 `AtomicU64` 无锁实现，Python 用 `threading.Lock`。行为一致但性能不同（Rust 更快）。Histogram 的 bucket 计数和分位数计算需精确对齐。

4. **audit canonical_json**：Rust `serde_json` 默认保留插入顺序（需 `preserve_order` feature），Python `json.dumps(sort_keys=True)` 强制排序。需确保 Rust 端也按 key 排序。

5. **backup sha256**：Rust `sha2` crate 与 Python `hashlib.sha256` 输出一致（都是标准 SHA-256 hex）。

## 6. 实现计划

### P0: health_check wire-production（最快完成，Rust 已有实现）

1. **PyO3 暴露**：在 `rust_ext/src/daemon_query.rs` 新增 `health_check_all` 函数，包装 `HealthChecker::check_all()`
2. **差分测试**：编写 D1 测试矩阵（7 个用例）
3. **wire-production**：`server/daemon_server.py` 的 `health` RPC 接入 Rust 短路 + fail-soft 降级
4. **verify + refresh + review**

### P1: metrics 纯计算

1. **Rust 实现**：在 `rust_ext/src/daemon_query.rs` 新增 `metrics_percentile` / `metrics_format_labels`
2. **差分测试**：编写 D2/D3 测试矩阵
3. **wire-production**：`server/metrics.py` 接入 Rust 短路
4. **verify + refresh + review**

### P2: audit 纯计算

1. **Rust 实现**：在 `rust_ext/src/daemon_query.rs` 新增 `audit_canonical_json` / `audit_compute_signature`
2. **差分测试**：编写 D4 测试矩阵
3. **wire-production**：`db/db_audit_chain.py` 接入 Rust 短路
4. **verify + refresh + review**

### P3: backup 纯计算

1. **Rust 实现**：在 `rust_ext/src/daemon_query.rs` 新增 `backup_compute_file_sha256` / `backup_compute_meta_checksum`
2. **差分测试**：编写 D5 测试矩阵
3. **wire-production**：`server/backup_restore.py` 接入 Rust 短路
4. **verify + refresh + review**

## 7. 优先级与平台限制

- **P0（health_check）**：Rust 已有实现，最快完成。Windows 兼容（内存检查降级到 Python）
- **P1（metrics 纯计算）**：中等复杂度，纯计算无 I/O
- **P2（audit 纯计算）**：低复杂度，但调用频率低
- **P3（backup 纯计算）**：最低优先级，daemon_server.py 的 backup/restore RPC 未使用 BackupManager

**Phase 4-4（systemd、双 UID、容器挂载与真实 Linux E2E）**：Linux 特定，Windows 上无法完成。仅编写 contract，实际 E2E 验证需在 Linux 环境执行。
