# Python→Rust 迁移完整性盘点：Python 侧残留 server/业务逻辑清单

- 日期：2026-09-09（第五阶段，承接 client_convergence_codereview_20260909.md）
- 判定标准（用户冻结）：收敛架构下 Python 只应剩 **薄 HTTP client + MCP 托管壳**；一切 server、数据层、业务引擎应迁移至 Rust daemon。
- 方法：全量静态盘点（文件规模、import sqlite3、socket 监听、route_rpc 使用、引用图追溯）。标注"死重"的模块仅表示**未发现静态引用**，删除前须动态引用行为验证。

## 一、总量画像

| Python 面 | 行数 | 性质 |
| --- | --- | --- |
| db/ 包（Python 数据层） | **56,354** | 迁移主体（G2） |
| server/*.py 顶层 | 21,446 | 合规薄壳 + 遗留 server 混杂 |
| cli/*.py | ~19,600 | CLI 展示层（软门禁覆盖） |
| analyzers/ | 3,443 | 本地分析引擎（G4） |
| 根级 config.py/install.py 等 | ~3,300 | 混合 |
| **合计** | **≈ 10.4 万行** | 目标态应仅剩薄壳 ≈ 1-2 万行 |

## 二、分类结论

### A. 已收敛（合规，保留）
- `server/tools/*` 全部薄壳（route_rpc 透传，硬门禁 0 违例）；`cw.py`；`cli/`（软门禁白名单存量）。
- `server/daemon_client.py`（4,011 行 HTTP client——行数偏大但属 client 面，残留 hunk 见 CR8）。
- `server/mcp_server.py`（MCP 托管壳；残留死 import 见 G7）；`server/_mcp_common.py`（get_db 已收敛为"仅配置读取"，无业务 SQL）。
- `server/daemon_autostart.py`（client 发现/autostart，含 CR15 bug）。

### B. compat 迁移中（有正式承接卡，不重复开卡）
- `server/compat_worker.py`（319）+ `compat_registry.py`（369）：daemon 经 CW_COMPAT_PYTHON 调起的 Python worker。**compat_registry 里注册的每个 handler 组就是"还没迁到 Rust 的业务"的权威清单**——迁移进度 = T-1787293451688 卡 + route matrix 17 项 KNOWN_DRIFT 清空。

### C. 遗留本地 server（迁移缺口，与"Python 无 server"直接冲突）

#### 🔴 G1：`server/daemon_server.py`（1,860 行）——第二个 server 仍可启动
- EnterpriseDaemonServer/EnterpriseDaemonService：**UDS socket 监听 + 直连 db_daemon SQL**（server 内唯一 bind/listen），经 `cli/daemon_commands.py:27-30` 被 `cw daemon serve` 活引用。
- 依赖簇：metrics.py（936）、snapshot_manager.py（420）、db/db_daemon。
- 判定：与 Rust daemon 并存的**影子 server**。HTTP 收敛后它是口径分裂源（UDS 路径 vs HTTP 路径行为可能漂移）。
- 建议：冻结写入面 → `cw daemon serve` 打废弃告警 → 行为等价核验后摘除（保留只读查询期）。

#### 🟡 G3：UDS/管道遗留传输面（约 1,600 行 + 6 文件引用）
- ipc_transport.py（578）、daemon_protocol.py（343）、UnixDaemonRpcClient——被 cli/daemon_commands.py、cli/main.py、server/daemon_client.py、agent_protocol.py、agent_watcher.py 引用。
- 建议：随 G1 摘除；daemon_client.py 内的 UDS 分支先降级为 fail-fast 报错。

### D. Python 侧业务引擎（应迁 Rust）

#### 🔴 G2：`db/` 包 56,354 行——最大单一迁移面
- Python 原生数据层（db.py、db_daemon、db_tasks、db_audit_chain、db_bootstrap…约 30 模块）。
- 现消费方仅：daemon_server.py（G1）与 compat_worker 间接面。**compat 迁移完成 + G1 摘除后，db/ 大部分转为死重**。
- 建议：不单独迁移 db/ 本身——它应随 G1 摘除 + compat 清空而自然退役；保留部分收敛为只读迁移工具。

#### 🟡 G4：`analyzers/`（3,443 行）本地分析引擎仍被活路径调用
- cli/main.py L12397（compile_commands import）、L12434（resolved_edges 本地计算）——**客户端本地跑业务分析**，绕过了"业务一律 daemon RPC"口径；server/job_handlers.py L225（issues）。
- 建议：resolved_edges/compile_commands/coverage/call_chain 在 daemon 侧已有对应能力（build_context.resolved_edges 等 RPC 存在），CLI 命令应改走 RPC，analyzers 退役。

#### 🟡 G5：疑似死重集群（未发现静态引用，摘除前需动态验证）
- job_executor（476）、job_handlers（332）、durable_staging（322）、staging_log（537）、health_check（694）、replicator（814）、backup_restore（852）、agent_watcher（685）、watcher（645）、audit_log（408）、snapshot_gc（407）、degraded_mode（334）、query_budget（301）、agent_protocol（373）、cli_admin、daemon_config（1,126）——合计约 **8,700 行**。
- 注意：daemon_config/daemon_autostart 有 client 侧合法部分（endpoint/manifest 解析），摘除前须拆分。

### E. 卫生项（一行级）
- 💭 G6：`normalize_structured_handoff` 在 cli/main.py:3687 存在**语义副本**（自称与 db/db_tasks 版本"语义一致"靠人工同步）——漂移风险；建议 daemon 暴露校验 RPC 或以冻结测试锚定双份一致性。
- 💭 G7：mcp_server.py:28 死 import `CodeGraphDB`（纯残留，删一行）；`_mcp_common.get_db` 的 CodeGraphDB 实例化是过渡态合法（仅配置读取），compat 清空后应一并退役。

## 三、建议迁移顺序

1. **G1 影子 server 下线**：先冻结写入面，`cw daemon serve` 打废弃告警，行为等价核验后摘除——它是口径分裂的源头（UDS 路径 vs HTTP 路径并存）。
2. **G3 遗留传输面随行摘除**：ipc_transport/daemon_protocol/UnixDaemonRpcClient 引用点先降级为 fail-fast 报错。
3. **G4 analyzers 活调用改走 RPC**：cli/main.py 两处 + job_handlers 一处，改动小、立刻消除"客户端本地跑业务分析"的口径破洞。
4. **compat 清空**：按既有承接卡推进，每迁走一个 handler，db/ 的活引用就少一个。
5. **G2 db/ 包退役**：依赖 1/4 完成后大部分转为死重，保留部分收敛为只读迁移工具。
6. **G5 死重清理**：动态引用行为验证后分批删除（遵守 Trash 优先/新 commit 回退原则）。
7. **G6/G7 卫生项**：一行级随手清。

## 四、与既有登记的关系

- **compat 迁移**（G2/B 面）：正式承接卡为 T-1787293451688-c14b1e44（MCP-007/008 等 Python→Rust native 迁移），本报告不重复开卡，仅提供 compat_registry handler 清单作为权威盘点口径。
- **route matrix 17 项 KNOWN_DRIFT**：即 B 面未迁清单的权威明细（verify_route_matrix.py 输出），随 compat 卡逐项清零。
- **CR8**（daemon_client.py PROJECT_ROOT/workspace_root 残留 hunk）：属 A 面收尾项，维持既有登记。
- 本报告 G1-G7 为**新登记缺口**，与上述卡片不重叠；建议 G1 单独开卡（涉及 serve 子命令废弃决策）。
