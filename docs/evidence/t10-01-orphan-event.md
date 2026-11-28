# T10-01 孤儿进程事件记录（epoch 1795849904）

- 事件: 第一次 solo run（`cargo test test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot --lib -- --test-threads=1`，start epoch 1795849125）的 Bash 外壳被 SIGTERM 终止后，测试二进制 `callwarden_core-d70a32c5d02997ee.exe`（PID 13828）未被连带终止，成为孤儿进程并持续膨胀至 **RSS 17.49 GB**（tasklist 外部采样），导致系统内存 96% 负载 / 0.95GB 可用，进而使 redo 编译 rustc OOM（见 t10-01-firsthang-solo-oom.log，"memory allocation of 49696512 bytes failed"）。
- 处置: tester 于 epoch 1795849904 taskkill 终止 PID 13828，内存恢复（52% / 14.82GB 可用）。
- 证据意义: **单个测试**（test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot）单独运行即触发进程内存膨胀至 17.49GB——最小复现集进一步收窄到单测试级别的直接证据。
- 方法教训: 后续所有 solo/定位跑一律经看门狗 runner 执行（8GB 阈值 + 进程树终止），不再用裸 Bash 重定向。
