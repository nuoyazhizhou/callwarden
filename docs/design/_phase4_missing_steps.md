# Phase 4 缺失项补充（Review 发现）

主设计 enterprise-daemon-shared-snapshot-plan.md §14 Phase 4 列出 10 条任务，原 task 只有 8 条。以下 2 条缺失，需补为子任务。

## 子任务

- [ ] 实现 diff_callers / diff_callees，基于 resolved edge delta 查询两个 snapshot 间的调用关系增删（file: rust_ext/）
- [ ] 实现 compare_snapshots 同步查询（小 scope 直接返回）+ 仓库级 diff 转后台 job（file: server/）
