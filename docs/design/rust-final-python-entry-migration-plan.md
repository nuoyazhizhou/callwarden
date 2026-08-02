# Rust 最终迁移与发布闭环计划

## 目标

将当前迁移主线剩余的 6 个功能/发布闭环，以及原先计划保留的 Python 生产入口，
全部推进到 Rust 主路径。Python 只允许作为一次性迁移工具、测试对照程序或明确
声明的外部进程，不得作为默认 fallback 隐式接管。

每个切片必须完成：

```text
Before-Edit Contract
→ Rust service/ABI
→ Python/Rust 进程级差分
→ 默认生产接线
→ 失败、超时、锁冲突、恢复测试
→ rollback 窗口
→ Linux/Windows/macOS 适用平台验收
→ cw refresh
→ 独立 review
```

实现 Agent 只能将任务推进到 `review`，不得自行 `apply/close`。

## 六个主线闭环

### 1. CLI 完整语义迁移

- 完成剩余 Rust `cw` 子命令的真实业务语义，不以统一统计报告代替命令实现。
- 逐命令复现参数、退出码、结构化 JSON、i18n 文案和错误码。
- 覆盖 console、agent、client、daemon 控制和 agent registry。
- 生产入口由 Rust CLI 直接执行；Python CLI 只作为差分基线。

### 2. Client/Agent 与 daemon 完整闭环

- 完成 Slice 6/7，覆盖 watcher、批量 refresh、恢复、重连和错误重试。
- Unix UDS、Windows named pipe、macOS launchd 端点统一使用 Rust transport。
- 完成真实 Linux 双 UID、容器挂载、SMB/VS Code 工作区和 daemon 崩溃恢复验收。

### 3. 默认切换与回滚窗口

- 为每个 Rust service 建立 rollback feature 和版本化 ABI。
- 默认入口只调用 Rust facade；Python fallback 只能显式配置启用。
- 记录命中率、失败原因、回滚次数和迁移版本，回滚不能静默吞异常。

### 4. 删除 Python fallback 与死代码

- 删除或隔离 parser、storage、build、query、CAS、watcher、daemon 的生产 fallback。
- 保留的 Python 代码必须标记为 reference、migration tool 或 external adapter，
  并由静态门禁防止生产 import。
- 增加 artifact inspector，确保冻结包不含不必要 Python grammar/runtime。

### 5. 发布与企业证据

- 生成 Windows amd64/arm64、Linux amd64/arm64/musl、macOS arm64 的真实包体、
  SHA256、SBOM、签名和 smoke 证据。
- 覆盖升级、回滚、schema migration、backup/restore、损坏输入和重新启动。
- CI 失败必须 fail closed，不允许用 skip 或历史报告伪造通过。

### 6. 最终 parity、灾备与独立复审

- Python/Rust 结果、数据库投影、调用图、错误码和审计记录逐项差分。
- 完成大规模性能、10 用户共享 CAS、watcher 延迟和多用户 ACL 验收。
- 独立 Reviewer 逐项 apply/close，最后再删除 legacy 入口。

## 原 Python 生产入口的最终迁移清单

以下 20 组全部纳入最终迁移范围。每组都必须有唯一 owner、Rust 模块、生产调用者
和独立差分测试；不能以“计划保留 Python”作为完成理由。

| 编号 | Python 入口组 | Rust 目标 | 关键验收 |
|---:|---|---|---|
| 1 | `cli/console.py` | Rust formatter/output | 中英文逐字符兼容 |
| 2 | `cli/agent.py` | Rust agent CLI | UDS、watcher、恢复 |
| 3 | `cli/client.py` | Rust client CLI | RPC、重试、错误码 |
| 4 | `cli/daemon.py`、`daemon_commands.py` | Rust daemon control | start/stop/status/restart |
| 5 | `cli/agent_registry.py` | Rust registry service | 合并规格、版本兼容 |
| 6 | `db_base.py`、`schema.py`、`db_migrate.py` | Rust StorageService | schema 幂等、损坏库、WAL |
| 7 | `db_build.py` 残余编排 | Rust BuildService | batch parse/write、失败回滚 |
| 8 | `db_query.py` 残余 SQL | Rust QueryService | 字段、排序、分页、预算 |
| 9 | `db_impact.py` 残余上层能力 | Rust impact service | review/vuln/diff 语义 |
| 10 | `db_evolution.py` | Rust evolution service | churn、hotspot、defect 关联 |
| 11 | `db_clone_detection.py`、`db_clone_groups.py` | Rust clone service | LSH、分组、增量 |
| 12 | `db_vector.py` 与 embedding 加载 | Rust vector/inference service | TopK、模型 ABI、无 Python runtime |
| 13 | `db_coverage.py`、`db_tests.py` | Rust test service | JUnit/LCOV/Cobertura、稳定性 |
| 14 | `db_git.py` | Rust git service | history、blame、commit、workspace 隔离 |
| 15 | `db_gc.py` | Rust GC service | archive、purge、CAS/snapshot GC |
| 16 | `db_lsp.py` | Rust LSP service | hover、definition、references |
| 17 | `server/daemon_client.py` | Rust RPC client | framing、peer identity、backpressure |
| 18 | `server/schema_migrator.py`、`backup_restore.py` | Rust migration/DR | backup、restore、upgrade、回滚 |
| 19 | `server/agent_watcher.py`、`agent_session.py` | Rust watcher/session | generation、stale、durable recovery |
| 20 | `server/audit_log.py`、MCP/Semgrep/RAG/XML 边界 | Rust governance/integration | audit chain、Semgrep、RAG、MCP 协议 |

### 外部依赖策略

- MCP：Rust 实现协议 server/client，Python FastMCP 仅作为差分基线，最终发布包不依赖
  Python MCP runtime。
- Semgrep：Rust 负责进程生命周期、超时、JSON/SARIF 解析、fail-closed 和审计；Semgrep
  本身仍是外部可执行工具，不把它伪装成 Rust 重写完成。
- embedding/RAG：Rust 负责模型 ABI、向量存储、TopK 和上下文编排；模型后端使用
  candle/ONNX 或明确版本化的 sidecar 协议，不默认导入 `sentence-transformers`。
- XML/LCOV/Cobertura/JUnit：Rust 使用 `quick-xml` 等解析器并保持字段级兼容。

## 执行顺序

1. 先冻结入口清单、生产调用图、ABI、rollback feature 和差分 fixture。
2. 先完成 Storage/CLI/client 基础，再迁移 Git、coverage、LSP、vector/RAG 和治理层。
3. 所有写路径迁移完成后，执行默认路由切换和 fallback 静态门禁。
4. 最后执行跨平台发布、灾备、性能、企业多用户 E2E 和独立复审。

禁止跨阶段批量标记完成；某组入口出现真实 Rust 异常时必须暴露失败并记录，不能
静默回退到 Python。

