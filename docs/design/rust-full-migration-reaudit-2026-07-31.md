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
| P0 | `T-1785427715194-719b1517` | agent/Python 会调用 `workspace.file.delete`，Rust daemon 无 dispatch/handler | open |
| P1 | `T-1785427715194-b201967a` | ACL 拒绝和管理员操作只留有 `TODO(audit)`，没有持久化审计 | open |
| P1 | `T-1785427715194-73f0f95d` | Phase 4-1 被关闭，但 7 个步骤的进度仍为 0/7，缺生产接线证据 | open |

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

该整改只证明 agent 事件循环不再是 stub。由于 daemon 删除 handler 尚缺失，不能据此声称 watcher 端到端闭合。

## 4. 后续复审顺序

1. 实现并验证 `workspace.file.delete` 的 ACL、generation、manifest/edge 清理和 snapshot 发布。
2. 完成 Rust `cw` 的真实生产命令迁移，禁止用统一 `not implemented` 骨架计为完成。
3. 补 ACL/admin audit 持久化与恢复测试。
4. 逐项复核 Phase 0、4、5、6 的已关闭任务。
5. 对 Phase 1、2、3、7 的未开始任务按依赖顺序实施，不能通过修改状态批量关闭。
