# Call Warden 全量 Rust 迁移自举计划

## 1. 目标与边界

本计划的目标是把 Call Warden 的**生产核心**逐步迁移到 Rust，并让迁移过程由 Call Warden 自身驱动：

- 用 `cw` 建立父任务、阶段任务和功能任务树；
- 用代码图追踪 Python/Rust 生产调用者；
- 每个功能先实现 Rust 版本，再接入一个真实生产入口；
- Python 与 Rust 并行对照，结果、错误和性能通过门禁后才切换；
- 切换后保留一个版本周期的回滚开关，再删除旧 Python 路径。

这里的“全量迁移”指生产核心和本地/企业运行时迁移到 Rust。MCP、Semgrep、RAG 模型等外部生态适配器可以保留为可选 Python adapter，不阻塞 Rust 核心发行包。

## 2. 不采用一次性重写

一次性把 `db/`、`server/`、`cli/` 全部翻译成 Rust 会破坏事务、ACL、generation、CAS 和查询兼容性，难以定位回归。每个功能采用垂直切片：

```text
契约 → Rust 实现 → Python/Rust differential test → 一个生产入口 → 灰度切换
     → 性能/安全/恢复验收 → 回滚窗口 → 删除旧路径
```

迁移任务不得只以“Rust 能编译”作为完成条件。

## 3. 目标架构

```text
Rust core
  ├─ parser / canonicalization
  ├─ SQLite / CAS / manifest / replicator
  ├─ GraphStore / snapshot / query
  ├─ watcher / generation / recovery
  ├─ daemon / UDS / ACL / metrics
  ├─ local / client / agent CLI
  └─ optional vector and clone algorithms

Python adapter (optional)
  ├─ MCP tool registration and JSON facade
  ├─ Semgrep process integration
  ├─ sentence-transformers / RAG integration
  └─ compatibility CLI during migration
```

Rust 核心通过稳定的内部 service trait 和 UDS RPC 暴露能力。Python adapter 不直接访问 SQLite、CAS 或 GraphStore，只调用 Rust service；这样可以避免迁移期间出现两套存储真相。

## 4. 每个功能子任务的统一完成协议

每个叶子任务都必须包含以下子步骤，缺一不可：

1. **Before-Edit Contract**：列出 Python 入口、Rust 目标 trait、输入输出、错误码、权限和事务边界。
2. **Rust implementation**：实现最小可用 Rust 模块，不复制无关逻辑。
3. **Golden/differential tests**：同一输入同时调用 Python 旧实现和 Rust 新实现，比较结构化结果、错误和边界行为。
4. **Production wiring**：只接入一个真实入口，并保留配置开关或回滚路径。
5. **Performance/security test**：记录 P50/P95、RSS、数据库写入量、队列和权限结果。
6. **Failure/recovery test**：覆盖进程崩溃、重复请求、旧 generation、损坏输入和锁冲突。
7. **Documentation and graph refresh**：更新架构、CLI/MCP/部署文档，执行 `cw --refresh-all`。
8. **Independent review**：实现 Agent 只能推进到 `review`，由另一 Agent/人工完成 apply/close。

## 5. 阶段任务树

### Phase 0：迁移基线、ABI 和自举门禁

先固定现有 Python 行为和 Rust 已有能力，建立 `migration_manifest`、结果 hash、错误码、性能基线和回滚配置。此阶段不删除 Python。

### Phase 1：Rust service/kernel 与存储真相

迁移数据库连接、schema migration、事务、workspace registry、CAS、manifest、replicator 和 SnapshotManager。完成后 Python 只能通过 facade 访问这些能力。

### Phase 2：Rust 图查询与构建管道

迁移符号/文件/调用边批量写入、resolver、GraphStore、搜索、callers/callees、call chain、循环检测和拓扑排序。以现有 1M/2M/10M 基准作为门禁。

### Phase 3：Rust watcher、generation 与恢复

迁移 inotify/FSEvents/ReadDirectoryChangesW 抽象、事件合并、秒级刷新、dirty overlay、staging log、retry log、崩溃恢复和 stale generation 拒绝。

### Phase 4：Rust daemon 与多用户安全边界

迁移 UDS framing、SO_PEERCRED、workspace ACL、资源预算、metrics、health、audit、systemd 生命周期和跨 UID E2E。

### Phase 5：Rust local/client/agent CLI

迁移 `cw` 的核心命令、client/agent、自动路由和安装路径；Python CLI 变成兼容 shim，逐命令灰度切换。

### Phase 6：分析能力与可选适配器

迁移 blast radius、演化热点、clone detection、向量相似度和测试关联中适合 Rust 的计算核心。Semgrep、模型下载和 MCP tool facade 暂保 Python adapter。

### Phase 7：默认切换、删除旧路径与发布

所有功能完成对照、灰度和回滚窗口后，默认发行 Rust-only core，删除生产 Python fallback，保留开发 reference 和独立 Python MCP adapter。

## 6. 依赖与禁止事项

- Phase 0 未完成，不得删除 Python 实现。
- 存储 schema、CAS 和 RPC 契约稳定前，不得并行迁移上层查询。
- watcher/generation 未完成恢复测试，不得宣称企业可用。
- 每个阶段必须有一个真实生产入口接入和一个可回滚版本。
- 迁移不以代码行数或测试数量作为唯一完成标准，必须追踪生产调用链。
- 不把 Python 与 Rust 同时写入同一业务表，避免双写分叉。
- 不为修复单个平台打包问题而改变跨平台 ABI；平台差异应隔离在 transport/build 层。

## 7. 总体验收门槛

### 功能

- 生产入口不再直接 import 被迁移的 Python 模块；
- Python/Rust differential fixtures 通过率 100%；
- 任务、workspace、CAS、snapshot、watcher、daemon 恢复链均有真实 E2E；
- 两个真实 UID 无法跨 workspace 越权；
- dirty overlay 不污染 Global CAS。

### 性能

- 与当前 Rust GraphStore 基线相比无回归；
- 1M/2M 符号构建和加载分别记录 stage timing；
- 单文件 watcher 更新 P95 < 3s；
- 10 用户 × 5 workspace 重复 parse 率 < 5%；
- 核心二进制不携带 Python runtime、PyInstaller、numpy、cryptography 或 OpenSSL 运行时。

### 发布

- Linux x86_64/aarch64、Windows x86_64/arm64、macOS arm64 均有真实 CI 产物；
- 每个平台执行 `--version`、`--help`、daemon/client/agent smoke；
- 包体、SHA-256、SBOM、schema migration、backup/restore 和升级回滚均有证据；
- Python MCP adapter 作为可选包发布，不影响 Rust-only 核心包。

## 8. 建议工期

在已有 Rust parser、GraphStore、daemon、SnapshotManager 基础上：

| 阶段 | 估算 | 主要风险 |
|---|---:|---|
| Phase 0 | 2～4 周 | 旧行为基线不完整 |
| Phase 1 | 6～10 周 | SQLite/CAS 事务和迁移 |
| Phase 2 | 8～14 周 | 查询语义和大规模性能 |
| Phase 3 | 6～10 周 | watcher、恢复和平台差异 |
| Phase 4 | 5～8 周 | 多用户安全与真实 Linux E2E |
| Phase 5 | 5～8 周 | CLI 兼容性与安装体验 |
| Phase 6 | 6～12 周 | 分析算法和 Python 生态边界 |
| Phase 7 | 3～6 周 | 清理旧路径和多平台发布 |

单人顺序执行约 12～20 个月；2～3 人并行且每阶段严格 review，约 6～10 个月。AI Agent 可以减少机械编码，但不能减少 differential test、恢复测试和独立审查工作。

## 9. 自举运行方式

父任务：`Call Warden 全量 Rust 迁移自举计划`

每个 Phase 建一个子任务；每个 Phase 再拆成按功能划分的子任务；每个功能子任务继续拆成“契约、实现、对照测试、生产接入、验收、文档”叶子步骤。实现 Agent 只推进到 `review`，由独立 Reviewer apply/close；父任务只在所有子树通过后自动完成。

任务开始前和每个阶段结束时执行：

```powershell
$env:PYTHONUTF8='1'
cw task status-tree <task-id>
cw --refresh-all
cw task completion-review <task-id>
```

这使迁移状态本身也由 Call Warden 持久化、可查询、可审计，避免迁移计划和真实代码状态再次分离。
