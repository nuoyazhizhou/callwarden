# CAS 发布与唯一 GC 协议

> 从 `enterprise-phase1-phase3-detail.md` v10.2 抽取。只保留当前规范 + 状态机 + 不变量 + 故障注入测试，不保留 v1-v9 修订过程。
> 基线版本：v10.2（`ad2e308`）。

## 1. CAS Key 设计

**唯一计算入口**（全文档不得重复定义）：

```python
def compute_cas_key_v1(content_hash, language, parser_version, callwarden_version,
                       extraction_config_version, abi_version, input_abi_version):
    """全文档唯一的 CAS key 计算函数。
    包含 abi_version（ParseFactV1）和 input_abi_version（ParseInputV1）。
    任一版本升级后旧 CAS 条目自然失效，不会被错误命中。
    """
    raw = (f"{content_hash}|{language}|{parser_version}|{callwarden_version}|"
           f"{extraction_config_version}|{abi_version}|{input_abi_version}")
    return hashlib.sha256(raw.encode()).hexdigest()
```

| 参数 | 来源 | 说明 |
|------|------|------|
| `content_hash` | `sha256(canonical_bytes)` | 规范化后的 UTF-8 bytes，非原始磁盘字节 |
| `language` | parser 检测 | `rust` / `c` / `python` / … |
| `parser_version` | tree-sitter grammar 版本 | `tree-sitter-c v0.24` |
| `callwarden_version` | `cw --version` | `0.2.0-p29` |
| `extraction_config_version` | SymbolRule/CallRule 配置 | 配置变更时手动 bump |
| `abi_version` | ParseFactV1 ABI 版本 | occurrence ID / parent / byte range / call ordinal 输出格式 |
| `input_abi_version` | ParseInputV1 ABI 版本 | canonical bytes / 编码 / 换行 / offset 坐标系 |

## 2. CAS Schema

```sql
CREATE TABLE IF NOT EXISTS cas_file_cache (
    cas_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL,
    callwarden_version TEXT NOT NULL,
    extraction_config_version TEXT NOT NULL,
    abi_version TEXT NOT NULL,
    input_abi_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',  -- building / ready
    parsed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,
    content TEXT NOT NULL              -- 符号正文文本
);

CREATE TABLE IF NOT EXISTS cas_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    local_symbol_id INTEGER NOT NULL,
    symbol_content_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    local_qualified_name TEXT NOT NULL,
    lexical_parent_local_id INTEGER DEFAULT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    start_byte INTEGER DEFAULT 0,
    end_byte INTEGER DEFAULT 0,
    visibility TEXT DEFAULT 'private',
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    FOREIGN KEY (symbol_content_hash) REFERENCES cas_symbol_contents(content_hash),
    UNIQUE(cas_key, local_symbol_id)
);

CREATE TABLE IF NOT EXISTS cas_raw_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    caller_local_id INTEGER DEFAULT NULL,
    caller_name TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    call_line INTEGER NOT NULL,
    call_ordinal INTEGER DEFAULT 0,
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)
);

CREATE TABLE IF NOT EXISTS cas_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    import_path TEXT NOT NULL,
    import_kind TEXT DEFAULT 'import',
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, import_path, import_kind)
);

-- GC 窗口期引用保护
CREATE TABLE IF NOT EXISTS cas_pending_refs (
    cas_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (cas_key, workspace_id)
);
```

**自包含约束**：
- `cas_symbol_contents` 只存 `content_hash + content`。
- `cas_symbols.start_byte/end_byte` 记录符号在文件中的字节偏移。
- 不再有任何跨库 FK（`symbol_content_hash` 指向 CAS DB 内部）。

**内容边界**：
- `cas_raw_calls` 存**单文件内** tree-sitter 能直接解出的 raw 调用文本（如 `foo()`、`std::vector::push_back`），不进跨文件 `call_edges`。
- `call_edges`（跨文件解析边）必须在 workspace/snapshot 层解析后归属，依赖 build context。

## 3. CAS 原子发布协议（四阶段）

```
┌─────────────────────────────────────────────┐
│ 阶段 1: 插入 building 状态                    │
│ INSERT INTO cas_file_cache ... state='building'
├─────────────────────────────────────────────┤
│ 阶段 2: 写入 payload                         │
│ INSERT cas_symbol_contents                   │
│ INSERT cas_symbols                           │
├─────────────────────────────────────────────┤
│ 阶段 3: 写入 raw calls + imports              │
│ INSERT cas_raw_calls                         │
│ INSERT cas_imports                           │
├─────────────────────────────────────────────┤
│ 阶段 4: 原子切换 ready                        │
│ UPDATE cas_file_cache SET state='ready'      │
└─────────────────────────────────────────────┘
```

```python
def cas_publish_with_retry(cas_key, parse_result, workspace_id, cas_conn, max_retries=3):
    """带 busy retry 的 CAS 发布 + pin 包装层。"""
    for attempt in range(max_retries):
        try:
            cas_conn.execute("BEGIN IMMEDIATE")
            publish_or_pin_in_transaction(cas_key, parse_result, workspace_id, cas_conn)
            cas_conn.execute("COMMIT")
            return
        except sqlite3.OperationalError as e:
            try:
                cas_conn.execute("ROLLBACK")
            except Exception:
                pass
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.1 * (attempt + 1))
                row = cas_conn.execute(
                    "SELECT state FROM cas_file_cache WHERE cas_key = ?", (cas_key,)
                ).fetchone()
                if row and row["state"] == "ready":
                    # 已 ready：只需补 pin
                    cas_conn.execute("BEGIN IMMEDIATE")
                    publish_or_pin_in_transaction(cas_key, None, workspace_id, cas_conn)
                    cas_conn.execute("COMMIT")
                    return
                continue
            raise
```

### 3.1 Refresh 流程中的 TOCTOU 修复

```
refresh 流程（带 flock 协调）：
1. 计算 content_hash + cas_key
2. 乐观查询（无锁）→ 命中 ready 则跳过
3. miss → parse（在 flock 外完成，避免阻塞 GC）
4. flock(LOCK_SH) → 事务内 recheck + publish_or_pin_in_transaction()
5. 短事务 COMMIT → unlock
```

**关键规则**：parse 必须在 `LOCK_SH` 之前完成，否则慢文件会长期阻塞 GC（GC 等 `LOCK_EX`，而 `LOCK_SH` 持有者在 parse）。

## 4. file_generations 两阶段 CAS（daemon 侧）

```sql
CREATE TABLE file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_session_id TEXT DEFAULT '',
    latest_session_epoch INTEGER DEFAULT 0,
    latest_seq INTEGER DEFAULT 0,
    latest_seen_generation TEXT DEFAULT '',     -- "{epoch}:{seq}"
    latest_committed_generation TEXT DEFAULT '', -- "{epoch}:{seq}"
    PRIMARY KEY (workspace_id, rel_path)
);
```

| 阶段 | 操作 | 事务 | 失败处理 |
|------|------|------|---------|
| **第一阶段：seen** | `BEGIN IMMEDIATE` → `UPDATE latest_seen_generation WHERE generation < incoming_gen`（0 rows → stale，ROLLBACK）→ `COMMIT` | 独立短事务 | stale seq 直接丢弃，不报错 |
| **第二阶段：committed** | `BEGIN IMMEDIATE` → manifest 提交 → `UPDATE latest_committed_generation WHERE latest_seen_generation = incoming_gen`（0 rows → stale，ROLLBACK）→ `COMMIT` | 独立短事务 | stale manifest commit 被条件 UPDATE 阻止 |

```
第一阶段 seen:
  BEGIN IMMEDIATE
    row = SELECT latest_seen_generation ... WHERE workspace_id=? AND rel_path=?
    IF incoming_gen <= latest_seen → ROLLBACK（stale，不报错）
    ELSE → UPDATE latest_seen_generation = incoming_gen
  COMMIT

第二阶段 committed:
  BEGIN IMMEDIATE
    UPDATE workspace_manifests ...
    UPDATE latest_committed_generation WHERE latest_seen_generation = incoming_gen
    IF rowcount != 1 → ROLLBACK（其他 handler 已覆盖 seen）
  COMMIT
```

## 5. 唯一 GC 协议

### 5.1 协议

```
LOCK_EX → BEGIN IMMEDIATE → scan manifests + pending refs → sweep → COMMIT → unlock
```

- **`LOCK_EX`**（flock）：阻塞所有 refresh 的 `LOCK_SH`，防止 scan 期间新 CAS 发布。
- **`BEGIN IMMEDIATE`**：保护 sweep 内部原子性。
- **fail-closed**：任何 workspace DB 读取失败 → ROLLBACK + 中止，不删任何条目。
- **不回收 active generation 的 cas_key**：`file_generations` 中引用到的 key 必须存活。

### 5.2 Python 实现

```python
def gc_cas(cas_conn, grace_period_days=7):
    """唯一权威 gc_cas 实现。
    协议：LOCK_EX → BEGIN IMMEDIATE → scan manifests + pending refs → sweep → COMMIT → unlock。
    """
    flock_fd = os.open(CAS_FLOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(flock_fd, fcntl.LOCK_EX)

        cas_conn.execute("BEGIN IMMEDIATE")
        try:
            # 阶段 1：扫描所有 workspace DB（持锁状态，fail-closed）
            live_keys = set()
            now = time.time()
            grace_threshold = now - grace_period_days * 86400
            scanned_workspaces = []

            for ws_db_path in glob.glob(os.path.join(cas_dir, "*", "callwarden.db")):
                try:
                    ws_conn = sqlite3.connect(f"file:{ws_db_path}?mode=ro", uri=True)
                    has_table = ws_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_manifests'"
                    ).fetchone()
                    if not has_table:
                        ws_conn.close()
                        continue
                    ws_mtime = os.path.getmtime(ws_db_path)
                    rows = ws_conn.execute(
                        "SELECT DISTINCT cas_key FROM workspace_manifests WHERE cas_key IS NOT NULL"
                    ).fetchall()
                    live_keys.update(r["cas_key"] for r in rows)
                    ws_conn.close()
                    scanned_workspaces.append((ws_db_path, ws_mtime > grace_threshold))
                except Exception as e:
                    cas_conn.execute("ROLLBACK")
                    print(f"GC aborted: workspace DB {ws_db_path} read failed: {e}")
                    return False

            # 阶段 2：pending_refs 加入 live set
            pending_keys = cas_conn.execute(
                "SELECT DISTINCT cas_key FROM cas_pending_refs WHERE expires_at > ?",
                (now,)
            ).fetchall()
            live_keys.update(r["cas_key"] for r in pending_keys)

            # 阶段 3：mark-sweep（同一事务内，先子表后正文表后父表）
            cas_conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gc_live (cas_key TEXT PRIMARY KEY)")
            cas_conn.execute("DELETE FROM _gc_live")
            cas_conn.executemany("INSERT OR IGNORE INTO _gc_live VALUES (?)",
                                 [(k,) for k in live_keys])

            # 3a. 先删子表
            cas_conn.execute("DELETE FROM cas_symbols WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
            cas_conn.execute("DELETE FROM cas_raw_calls WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")
            cas_conn.execute("DELETE FROM cas_imports WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)")

            # 3b. 再删正文表
            cas_conn.execute("""
                DELETE FROM cas_symbol_contents
                WHERE content_hash NOT IN (SELECT DISTINCT symbol_content_hash FROM cas_symbols)
            """)

            # 3c. 最后删父表（只删 ready）
            cas_conn.execute("""
                DELETE FROM cas_file_cache
                WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live) AND state = 'ready'
            """)

            # 3d. 清理孤儿 building 条目（崩溃残留）
            cas_conn.execute("DELETE FROM cas_symbols WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
            cas_conn.execute("DELETE FROM cas_raw_calls WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
            cas_conn.execute("DELETE FROM cas_imports WHERE cas_key IN (SELECT cas_key FROM cas_file_cache WHERE state = 'building')")
            cas_conn.execute("DELETE FROM cas_symbol_contents WHERE content_hash NOT IN (SELECT DISTINCT symbol_content_hash FROM cas_symbols)")
            cas_conn.execute("DELETE FROM cas_file_cache WHERE state = 'building'")

            # 3e. 清理过期 pending_refs
            cas_conn.execute(
                "DELETE FROM cas_pending_refs WHERE expires_at <= ?",
                (now,)
            )

            cas_conn.execute("DROP TABLE _gc_live")
            cas_conn.execute("COMMIT")
            return True
        except Exception:
            cas_conn.execute("ROLLBACK")
            raise
    finally:
        fcntl.flock(flock_fd, fcntl.LOCK_UN)
        os.close(flock_fd)
```

### 5.3 Rust 实现（G6 落地）

`rust_ext/src/daemon/cas.rs::CasStore` 实现 fs2 flock + BEGIN IMMEDIATE 双保险：

```rust
pub fn gc(&self, live_keys: &HashSet<String>, _grace_period_days: f64)
    -> Result<CasGcStats, CasPublishError>
{
    // G6: 先获取文件锁（RAII，drop 自动释放）
    // 内存模式（db_path = None）跳过 flock，返回 Ok(None)
    let _gc_lock = self.acquire_gc_lock()?;

    let conn = self.conn.lock().unwrap();
    conn.execute_batch("BEGIN IMMEDIATE")?;
    // ... mark-sweep 实现（同 Python）...
}
```

**关键设计**：

| 维度 | Python 实现 | Rust 实现（G6） |
|------|------------|------------------|
| 文件锁 API | `fcntl.flock(fd, LOCK_EX)` | `fs2::FileExt::lock_exclusive(&file)` |
| 锁路径 | `CAS_FLOCK_PATH`（全局） | `{db_path}.lock`（per-DB） |
| RAII | `try/finally` + 显式 `LOCK_UN` | `GcLockGuard` struct + `Drop` trait |
| 跨平台 | 仅 Unix（fcntl） | 跨平台（fs2 在 Windows 用 LockFileEx，Linux 用 flock） |
| 内存模式 | 不适用 | `db_path = None` 时跳过 flock（单进程内 Mutex 串行化） |
| 失败策略 | 抛异常 | `Err(CasPublishError::Lock)`，不降级 |
| BEGIN IMMEDIATE | 是 | 是（双保险：flock 防跨进程，BEGIN 防同进程线程并发） |

**fs2 crate 选择原因**：
- 跨平台：Windows（`LockFileEx`）+ Linux（`flock`）+ macOS（`flock`）一致 API
- 不依赖 libc 直接系统调用，避免 unsafe 代码
- API 简洁：`lock_exclusive`（阻塞）/ `try_lock_exclusive`（非阻塞）/ `unlock`

**降级策略**：
- fs2 调用失败 **不降级**，直接返回 `Err(CasPublishError::Lock)`
- 原因：GC 是破坏性操作，宁可失败不可不安全
- 调用方（`cw_daemon` 或 `Replicator`）负责重试或上报错误

**测试覆盖**（5 个新测试）：
- `test_gc_in_memory_skips_flock`：内存模式跳过 flock
- `test_gc_file_mode_creates_lock_file`：文件模式创建 `.lock` 文件
- `test_gc_file_mode_concurrent_lock_blocks_or_fails`：并发互斥验证
- `test_gc_with_file_mode_succeeds`：gc() 完整流程
- `test_gc_unreferenced_with_file_mode_succeeds`：gc_unreferenced() 完整流程

### 5.4 删除顺序（不变量）

```
cas_symbols → cas_raw_calls → cas_imports   （子表，先删）
    ↓
cas_symbol_contents                          （正文表，无符号引用的正文可删）
    ↓
cas_file_cache                               （父表，只删 state='ready'）
    ↓
building 孤儿清理                             （子表→正文→父表，同顺序）
    ↓
cas_pending_refs（TTL 过期）                  （最后清理）
```

## 6. 不变量

| # | 不变量 | 来源 |
|---|--------|------|
| C1 | `cas_key` 包含 `abi_version` 和 `input_abi_version`，任一升级后旧条目自然失效 | `compute_cas_key_v1` |
| C2 | CAS 只存单文件粒度（`raw_calls_in_file`），不存跨文件 `call_edges` | Schema 设计 |
| C3 | CAS 原子发布四阶段：building → payload → raw calls → ready，崩溃后 building 被 GC 清理 | 发布协议 |
| C4 | content_hash 基于 canonical bytes（规范化后），不基于原始磁盘字节 | `canonicalize_source` |
| C5 | GC 不回收 active generation 的 cas_key | `file_generations.latest_seen_generation` |
| C6 | GC fail-closed：任何 workspace DB 读取失败 → ROLLBACK + 中止，零删除 | `gc_cas` |
| C7 | GC delete order：子表→正文表→父表→building 孤儿→pending_refs TTL | sweep 顺序 |
| C8 | refresh parse 在 `LOCK_SH` 之前完成，避免慢文件阻塞 GC | flock 协调 |
| C9 | `cas_pending_refs` 保护 manifest 提交窗口期，TTL 防永久残留 | `expires_at` |
| C10 | `file_generations` 第二阶段条件 UPDATE 拒绝 stale manifest commit | `latest_seen_generation = incoming_gen` |

## 7. 故障注入测试

| 场景 | 注入方式 | 期望结果 |
|------|---------|---------|
| CAS 发布中途崩溃 | 在阶段 2/3 之间 kill 进程 | 重启后 building 条目被 GC 清理，CAS 无半成品 |
| GC 期间 refresh 并发 | 持 `LOCK_EX` 期间另一个进程尝试 refresh | refresh 在 `LOCK_SH` 阻塞直到 GC 完成 |
| workspace DB 不可读 | 扫描时删除一个 ws DB 文件 | fail-closed → ROLLBACK + 中止，零删除 |
| pending_refs 过期 | TTL 已过 + 无 manifest 引用 | GC 清理过期 pending_refs |
| 两阶段 CAS 的第二阶段 stale | S2 在 S1 第一阶段后推进 seen → S1 第二阶段条件 UPDATE 0 rows | ROLLBACK，manifest 不提交 |
| ABI 版本升级后 | bump `abi_version` → 新旧 cas_key 不同 | 旧条目不被命中，GC 自然清理 |
| building 条目泄漏 | 进程 crash 后残留 state='building' + 子表记录 | GC step 3d 先清理子表再删 building 父记录 |
