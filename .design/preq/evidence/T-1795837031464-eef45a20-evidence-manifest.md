# T-1795837031464-eef45a20 · 证据清单（T11 test-only 投影测试补强）

| # | 文件 | sha256 | 说明 |
|---|------|--------|------|
| 1 | T-1795837031464-eef45a20-AC.md | 10e6a390149270d769c705f4407f7dbe3cec477d507d32b714793c9b52432ba8 | AC 证据文档（范围/环境/红绿验证/断言对照/结果/风险） |
| 2 | T-1795837031464-eef45a20-red-round1.log | 9893133174b74b9a907b59735aee45734fea85a4702a132cd70baddf32ca5c0f | 红轮 1：终态短路 ×2 + hard_unmet break mutation → 4 FAILED，CARGO_RC=101 |
| 3 | T-1795837031464-eef45a20-red-round2.log | c651d0f4f8a52fa3e8fc346c5240252d11c9f992e1e227e902ca4e5f771aae76 | 红轮 2：无 binding 分支 decision BLOCKED→COMPLETE mutation → 1 FAILED（COMPLETE != BLOCKED），CARGO_RC=101 |
| 4 | T-1795837031464-eef45a20-green-test.log | 9a0e70972b2769ddce40b9541b3a639d88d856842645ee11dd3abe258dfcf076 | 绿轮：生产逻辑恢复 → 10 passed / 0 failed，CARGO_RC=0 |
| 5 | T-1795837031464-eef45a20-full-test.log | 7aa99ad75d6256575396da103fcc25cd1b8ecccd10560473b8a73bca82b77e53 | 全量回归（skip snapshot_state）：2105 passed / 1 failed / 62 filtered out，CARGO_RC=101 |
| 6 | T-1795837031464-eef45a20-rerun-flaky.log | 81a1b6cf93b47606cafbcb05f71b55d602047668e2a4a2fcc6d0ac02cd8fa9be | 唯一失败项 decompress bomb 单独重跑：ok，CARGO_RC=0（资源压力型假失败闭环） |
| 7 | T-1795837031464-eef45a20-tests-keylines.txt | d2dc9341c91695da3561d7641c319cc808a7edfaed4bc810d34b4e809d0a00d6 | 红绿两轮 + skip 全量 + flaky 重跑关键行 |

## 交付物

- `rust_ext/src/daemon/task_loop/next_action_test.rs` +117 行（3 测试：closed/reverted 终态投影[] / 多依赖投影）
- `rust_ext/src/daemon/task_collab_tests_projection.rs` +72 行（1 测试：无 binding 两层 fail-closed）
- 提交 hash：（见下方补录）

## 治理记录

- 任务：T-1795837031464-eef45a20（REQ-PREQ-01，闭合批次 1 验收缺口：T2-01/T2-02/T3-01）
- step：S-1795837031479-efd46b10（implement，symbol prerequisite_projection_edges）
- lease：L-1a96f16aaaf6165e（fencing_counter=2，前序 L-f25e4d86fff5d609 fencing=1 因会话中断过期后重新 acquire）
- 身份：t11-exec-3c052497 / sess-t11-exec-3c052497 / inst-t11-exec-3c052497
- 基线：run/preq-t6-integration @ 78d7ddc
