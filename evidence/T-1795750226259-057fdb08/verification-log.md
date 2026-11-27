# R1（F1 整改）verification log

Task: T-1795750226259-057fdb08 — fs_handlers.rs::apply_file_events 空符号组跳过符号重建与版本镜像

## 改动

- `rust_ext/src/daemon/fs_handlers.rs::apply_file_events`（Inserted 分支重建闭包）：
  - 收集 `syms` / `calls` 后，`if syms.is_empty() { return Ok(()); }`——空符号组不执行
    「DELETE symbols + 版本镜像重建 + 调用边写入」，与旧实现
    `parse_and_store_symbols` 空符号在 BEGIN IMMEDIATE 前早退返回 (0,0) 的语义对齐。
  - 闭包前 `collect_file_symbol_names` 与闭包后 `collect_file_symbol_names` +
    `collect_file_raw_callee_names` 照旧无条件执行（affected_names 行为与旧主循环一致）。
- 新增测试 `test_apply_file_events_empty_symbols_preserves_old_rows`：先有符号版本走旧路径，
  再分别以旧/新路径写入空符号内容，断言旧 symbols 行保留一致。

## 验证（命令原文 + 关键输出）

```
cargo test --lib test_apply_file_events_empty_symbols_preserves_old_rows
→ test daemon::fs_handlers::tests::test_apply_file_events_empty_symbols_preserves_old_rows ... ok
→ test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 2145 filtered out; finished in 0.47s

cargo test --lib daemon::fs_handlers
→ test result: ok. 58 passed; 0 failed; 0 ignored; 0 measured; 2088 filtered out; finished in 5.64s
```

## 结论

- 空符号组不再清空旧 symbols/calls/file_versions，新旧路径行为等价（「空符号不清旧数据」约束恢复）。
- fs_handlers 模块全绿（58 passed / 0 failed），无回归。

Date: 2026-11-27 14:55 (UTC+8)
