# 十项 Rust 迁移完整闭环与企业验收计划

## 目标

将十项 Rust 迁移任务从“Rust 切片存在且 focused 测试通过”推进到真正可关闭：

```text
契约冻结 → Rust service → Python/Rust differential → 默认生产入口
→ rollback 窗口 → 性能/安全/恢复 → 企业环境 E2E → 独立 review
```

MCP、Semgrep、RAG 等适配器可以继续保留 Python；SQLite、CAS、manifest、replicator、snapshot、build、query、watcher 和 CLI 核心不能保留未经审计的 Python 主路径。

## 十个工作包

| 顺序 | 工作包 | 关闭条件 |
|---:|---|---|
| 1 | E5 外部集成与运维命令 | Rust CLI 覆盖全部声明命令，超时/资源限制/平台差异/错误码与 Python 差分通过，Linux/Windows/macOS smoke 通过 |
| 2 | SQLite StorageService | Rust 统一连接、schema migration、WAL、事务和恢复；`db_base` 默认只经 Rust facade；损坏库、锁冲突、迁移幂等通过 |
| 3 | Global/Local CAS | Rust 统一 publish/lookup/pin/pending refs/GC；Python replicator 不再直接写 CAS；partial/ready、崩溃窗口和跨 workspace 隔离通过 |
| 4 | Manifest/refresh commit | Rust 统一 manifest、projection、generation CAS、refresh commit；Python 只传 DTO，不直接写业务表；clean/dirty/stale/rollback 通过 |
| 5 | Replicator/Snapshot/backup | Rust 统一 CAS merge、snapshot publish/load/GC、full/db-only backup/restore；daemon 重启和损坏备份恢复通过 |
| 6 | BuildService/ParseFact/symbol/call | Rust 统一批量注册、解析 ABI、file version、symbol、raw call 写入；Python build 只做编排；全语言和失败回退矩阵通过 |
| 7 | Edge/CSR/GraphStore | Rust 统一 resolve、CSR、workspace 过滤和图构建；增量边删除、循环、拓扑和并发 snapshot 发布通过 |
| 8 | Query service | local/enterprise/auto 的 search、symbol、callers、callees、chain、cycles、topo 输出逐命令差分；预算、ACL、空 snapshot 和大结果集通过 |
| 9 | 性能与大规模验收 | 1M/2M 基线、GraphStore RSS、clone LSH、增量刷新和 10 用户共享指标真实记录；无回归门禁，不以外推代替实测 |
| 10 | Watcher/企业 E2E | Linux inotify、macOS、Windows adapter；单文件 P95<3s、批量 checkout/repo sync、stale generation、崩溃恢复、双 UID/容器/SMB 验收通过 |

## 依赖顺序

1. 工作包 2 → 3 → 4 → 5。
2. 工作包 6 依赖 2、3、4；工作包 7 依赖 6；工作包 8 依赖 7。
3. 工作包 9 依赖 6、7、8；工作包 10 依赖 3、4、5、8。
4. 工作包 1 可并行，但最终发布切换必须等待 1–10 全部 review 通过。

## 统一验收证据

每个工作包必须提交：

- Before-Edit Contract、owner 文件清单和 rollback feature；
- Python/Rust 同 fixture 结构化差分结果；
- 默认生产入口调用链证据，包含反向调用者搜索；
- 功能、性能、安全、锁冲突、损坏输入、重复请求和崩溃恢复测试；
- Linux x86_64/aarch64、Windows amd64/arm64、macOS arm64 适用的 CI 产物或明确 skip 原因；
- `cw refresh` 成功记录和文档状态；
- 实现 Agent 只能推进到 `review`，由独立 Reviewer apply/close。

## 企业验收矩阵

| 场景 | 必须证据 |
|---|---|
| 10 用户 × 5 workspace × 同 repo | 重复 parse 率 <5%，第二 clean workspace 主要复用 CAS/manifest |
| clean/dirty/stale | dirty overlay 不进入 Global CAS，旧 generation 不覆盖新状态 |
| Linux 多 UID | workspace owner ACL、socket/daemon 权限、跨 UID 查询/写入拒绝 |
| Ubuntu 14.04–24.04 容器 | 宿主挂载 `/opt`、`/home`，SMB 和 VS Code 工作区路径都通过 |
| watcher | 单文件 P95<3s，批量事件合并，daemon 崩溃后 durable log 恢复 |
| 灾备 | backup/restore、schema migration、GC、损坏备份 fail-closed |
| 发布 | 六平台 smoke、包体/SHA256/SBOM、升级/回滚、无 Python runtime 的 Rust core |

## 禁止提前宣称完成

- 只有 PyO3 函数或局部单测，不能证明 service 完成；
- Python 仍直接写同一业务表时，不能宣称 Rust 主路径切换完成；
- focused 测试通过但真实 Linux/多 UID/大规模指标缺失时，不能宣称企业可用；
- 原任务状态为 `in_progress` 或 `review` 时，不能写成 `closed`。
