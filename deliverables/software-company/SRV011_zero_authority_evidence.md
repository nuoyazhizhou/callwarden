# SRV-011 Zero-Authority Evidence Manifest

任务：`T-1787323461285-e8a7a12c`（SRV-011 [runtime_projection]：server job executor Python authority → Rust daemon）
步骤：`S-1787323461287-e8bdf65c`（`zero_authority_evidence`）
Executor：`executor-workbuddy-v1-cur`
Session：`dcf88a76-0895-4f09-9245-1cc8cbaedb82`
Model：`workbuddy`
Workspace：`workspace_id=376`，`workspace_instance_id=4baea3ff12c2ea5c`，root=`C:\git_work\callwarden`

## 合同与提交锚点

| 项 | 值 |
|---|---|
| Task Contract | `TC-T-1787323461285-e8a7a12c` rev2 |
| Task Contract hash | `sha256:60d6648fdb7c16c63ef79d3a7e18d5fbb2d4f1cc56409f564188510f4e4f20db` |
| Role Contract | `rcl-T-1787323461285-e8a7a12c-executor` rev1 |
| Role Contract hash | `sha256:edb8b2d7d2dd5178953059eaf15e5a631f622cd0eb139f20fd3454470fa5b6c0` |
| Canonicalization hash | `sha256:59ad755be8740794624c927294f95515d2b17790ffc21b58f6d9cf7155ff188d` |
| Pre-migration AST baseline | `578da112401d0f02d450363fb6d702dbdfa826f8` parent blob |
| Rust authority commit | `578da112401d0f02d450363fb6d702dbdfa826f8` |
| Python attribution commit | `55817f1f1bea2e5a56aaac73efe712ce5c77376f` |
| Negative matrix commit | `45b305f1eced3125592462aca9a577ecb625c84e` |
| Evidence capture HEAD | `a5ff6a3fb6f2427aea75899357d3f8b7514b6277` |

白名单：`server/job_executor.py`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/http_server.rs`、`rust_ext/src/daemon/job_executor_handlers.rs`、`tests/test_srv_011.py`、`deliverables/software-company/`。本步骤只新增本证据文件；工作树中 `http_server.rs` 的既有格式化偏离未纳入本任务提交。

## 检查项

### 1. Python authority AST 盘点

使用 Python `ast` 对 `server/job_executor.py` 的调用节点进行前后扫描，禁用词集合为 `sqlite3.connect`、`get_db`、`SELECT`、`PRAGMA`、`init_jobs_schema`：

| 版本 | 命中 |
|---|---|
| before（`578da11^`） | `L193 sqlite3.connect`；`L213 init_jobs_schema` |
| after（HEAD） | `L214 sqlite3.connect`；`L234 init_jobs_schema` |

结论：生产 authority 已迁移，但 `JobExecutor.start` 的进程内兼容生命周期仍保留 SQLite 初始化，原因是现存 Phase 7 测试合同明确锁定 `test_start_inits_schema` / `test_start_idempotent`，且本卡的负矩阵将生产 authority 指向 Rust daemon。该残留不是新增生产调用链，必须由后续 repository-wide/SRV-019 gate 决定最终下线；本证据不把 AST 残留虚报为零。

### 2. Rust authority 与 HTTP capability

- `rust_ext/src/daemon/job_executor_handlers.rs::handle_start` 承接 jobs DB 打开、批次 10 PRAGMA、`JOBS_SCHEMA_DDL` 和 schema/index 核验，统一返回 `source=rust` 与 fail-soft reason。
- `rust_ext/src/daemon/dispatch.rs` 注册 `mcp.job_executor.start` → `handle_start`。
- `rust_ext/src/daemon/http_server.rs` 注册 capability `mcp.job_executor.start`，backend=`rust_native`，status=`available`，operation=`write`，owner=`T-1787323461285-e8a7a12c#SRV-011`。
- 生产长任务执行权威由 Rust `job_runner.rs` 的 `task.job_submit` / `task.wait_for_job` 承担；本 Python executor 不作为生产 fallback。

### 3. 负矩阵与存量回归

| 命令 | 结果 |
|---|---|
| `python -m pytest tests/test_srv_011.py -q` | **15 passed** |
| `python -m pytest tests/test_phase7_job_executor.py -q` | **17 passed** |
| `cargo test --manifest-path rust_ext/Cargo.toml job_executor_handlers --lib` | SRV-011 handler tests通过（7 tests，已有提交 `578da11`） |

SRV-011 测试覆盖 success、invalid、authority、unavailable、restart；真实 daemon runtime 段通过，未发生 skip。

### 4. 真实 HTTP runtime 指纹

`HttpDaemonRpcClient` 真实调用结果：

```json
{
  "ping": {
    "status": "ok",
    "pid": 5480,
    "transport": "http",
    "protocol_version": 1,
    "authority_id": "LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/7ce7ccb824889fbc03805083002c3c2a7a61512ca224a3a572d9b09007e9692a",
    "task_db_fingerprint": "7ce7ccb824889fbc03805083002c3c2a7a61512ca224a3a572d9b09007e9692a"
  },
  "mcp.job_executor.start": {
    "schema_ready": true,
    "jobs_table": true,
    "index_count": 4,
    "source": "rust"
  }
}
```
测试还核验了 jobs DB 的 WAL、父目录创建、幂等重启、已有数据保留，以及缺参/目录路径的 fail-soft 拒绝。

## handoff manifest

本步骤报告完成后，按固定路由交给独立 Reviewer：

```text
from_role: executor
outcome: executor_ready_for_review
next_role: reviewer
next_action: 独立复现 mcp.job_executor.start 的 HTTP success/invalid/restart 矩阵，核对 Rust dispatch/capability 和 Python residual AST finding，并确认残留仅为 Phase 7 兼容生命周期而非生产 fallback。
reason: SRV-011 的 Rust authority、HTTP capability、负矩阵和 runtime fingerprint 已有可复核证据；AST 结果如实记录了现存兼容层残留，供 Reviewer/最终 SRV-019 gate 判定。
independence_requirement: required
```
