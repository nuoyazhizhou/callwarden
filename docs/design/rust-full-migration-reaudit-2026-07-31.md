# Rust 全量迁移自举计划独立复审（2026-07-31）

## 1. 结论

“自举迁移计划已经完成”的声明不成立。

复审开始时，父任务 `T-1785147987425-07c09f75` 仅完成 105/224 步；Phase 1、2、3、7 均为 0/28。部分已关闭任务也没有满足计划规定的 production wiring、failure/recovery 和 independent review 条件。

本复审不以任务状态、勾选项或孤立单测作为完成证据。每项必须追踪到生产入口、权限/事务边界、故障恢复和发布产物。

## 2. 已确认问题

| 优先级 | 修复任务 | 发现 | 当前状态 |
|---|---|---|---|
| P0 | `T-1785427715161-888e304c` | Rust `cw` 除少数骨架命令外仍返回 `not implemented` | open |
| P0 | `T-1785427715192-ec391036` | Rust `cw-agent` 握手后打印 watcher stub 并退出 | 已实现，待独立 review |
| P0 | `T-1785427715194-719b1517` | agent/Python 会调用 `workspace.file.delete`，Rust daemon 原先无 dispatch/handler | 已实现，待独立 review |
| P1 | `T-1785427715194-b201967a` | ACL 拒绝和管理员操作只留有 `TODO(audit)`，没有持久化审计 | open |
| P1 | `T-1785427715194-73f0f95d` | Phase 4-1 被关闭，但 7 个步骤的进度仍为 0/7，缺生产接线证据 | open |
| P1 | `T-1785431356786-f981a60b` | daemon config 测试并行修改 `CW_DAEMON_*`，完整回归偶发失败 | 已修复，待独立 review |

## 3. 首项整改证据

`cw-agent` 已接入真实 Unix watcher 循环：

- created/modified 事件通过 Rust canonicalization 后调用 `workspace.file.refresh`；
- 大文件通过 FD 发送，发送前回卷到 byte 0；
- removed/renamed 事件发送 workspace-scoped `workspace.file.delete`；
- session 失效后重新连接并重试；
- SIGTERM/SIGINT 触发 watcher 停止和 PID 文件清理。

验证结果：

```text
Windows cargo check --bin cw-agent: PASS
WSL/Linux cargo check --bin cw-agent: PASS
WSL/Linux cargo test --bin cw-agent --no-default-features: 12 passed
```

该整改证明 agent 事件循环不再是 stub；后续 P0 已补齐 daemon 删除状态链，证据见下一节。root + 双真实 UID + 真实 UDS/inotify 仍是部署环境验收门禁。

## 4. 后续复审顺序

1. 实现并验证 `workspace.file.delete` 的 ACL、generation、manifest/edge 清理和 snapshot 发布。
2. 完成 Rust `cw` 的真实生产命令迁移，禁止用统一 `not implemented` 骨架计为完成。
3. 补 ACL/admin audit 持久化与恢复测试。
4. 逐项复核 Phase 0、4、5、6 的已关闭任务。
5. 对 Phase 1、2、3、7 的未开始任务按依赖顺序实施，不能通过修改状态批量关闭。

## 5. `workspace.file.delete` 契约

删除请求必须包含：

- `workspace_instance_id`
- `rel_path`
- `agent_session_id`
- `session_epoch`
- `monotonic_seq`

服务端按以下顺序处理：

1. 用 `SO_PEERCRED.uid` 校验 workspace owner。
2. 校验 active session，并用 `(session_epoch, monotonic_seq)` 执行 generation CAS；旧 session 或旧序号不得删除新状态。
3. 在 CodeGraph DB 的单个 `BEGIN IMMEDIATE` 事务中：
   - 删除目标文件符号发出的 calls；
   - 将其他文件指向目标符号的 `callee_id` 清零，保留待解析的调用文本；
   - 删除目标文件当前 symbols；
   - 将 `file_instances.status` 标为 `deleted`，保留文件和符号版本历史；
   - 若存在 `file_versions`，将当前版本标为 deleted；
   - 删除目标 workspace 的 manifest 行。
4. 不删除 `file_contents`、`symbol_contents` 或 CAS 条目；这些内容可能被历史版本和其他 workspace 共享。
5. 删除操作写入 durable staging；崩溃重放必须幂等。

## 6. `workspace.file.delete` 整改证据

P0 `T-1785427715194-719b1517` 拆为事务契约和 RPC/snapshot 两个子任务实施：

- `delete_workspace_file_from_codegraph` 在单个 `BEGIN IMMEDIATE` 事务内执行 workspace-scoped tombstone，保留共享内容和历史；
- dispatch、`WorkspaceDaemonState` 与 `SnapshotDaemonState` 已接入 `workspace.file.delete`；
- active session、epoch、monotonic sequence 和 workspace owner 在写入前校验；
- durable staging 记录 `delete` 操作，daemon 崩溃后在 snapshot 发布前幂等重放；
- 发布后的 GraphStore snapshot 不再返回已删除文件的符号；
- 跨 UID、stale session、无 active session 和 stale sequence 均被拒绝或丢弃。

验证结果：

```text
cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib
test result: ok. 516 passed; 0 failed

CARGO_TARGET_DIR=/tmp/callwarden-target cargo test \
  --manifest-path rust_ext/Cargo.toml --bin cw-agent --no-default-features --quiet
test result: ok. 12 passed; 0 failed
```

尚未完成的环境门禁是 root + 双真实 UID + 真实 UDS/inotify E2E；它不阻塞删除状态链进入独立代码 review，但阻塞“企业部署验收完成”的声明。

## 7. daemon config 测试隔离整改

完整 daemon 回归在同一代码版本上出现过 `515/516` 和 `514/516` 的偶发结果。根因是多个 config 测试并行修改进程级 `CW_DAEMON_*` 环境变量，其中 invalid-workers 用例可被 socket/template 用例读到。

P1 `T-1785431356786-f981a60b` 增加测试级共享锁和 panic-safe 环境守卫：进入用例时保存并清空全部 daemon 环境变量，退出时恢复原值。验证结果：

```text
daemon::config::tests::test_apply_env_overrides 子集连续 10 轮：PASS
cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib
test result: ok. 516 passed; 0 failed
```

该任务只修复测试证据的确定性，不改变生产环境变量读取逻辑，保持在 `review` 等待独立复核。
6. 数据库删除和 snapshot 发布成功后才提交 generation；失败时允许同一 generation 重试。

验收至少覆盖：非 owner、stale session、stale sequence、幂等删除、同路径跨 workspace 隔离、事务失败回滚、删除后 snapshot 查询不可见、崩溃恢复。
