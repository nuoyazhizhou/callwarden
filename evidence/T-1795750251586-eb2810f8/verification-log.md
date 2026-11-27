# R4（F4 整改）verification log

Task: T-1795750251586-eb2810f8 — fs_handlers.rs::commit_batch 补批回滚与幂等重放直接单测

## 改动

- 新增测试 `test_commit_batch_db_error_rolls_back_and_replays`（fs_handlers.rs 测试区，
  F3 对账测试之后），覆盖 plan §Verification V5 的两个断言点：
  1. **批回滚**：DB 触发器注入必失败组（`BEFORE INSERT ON symbols WHEN NEW.symbol_hash
     LIKE 'b.py-%' → RAISE(ABORT, 'injected db failure')`，实测 RAISE(ABORT) 穿透
     INSERT OR IGNORE），批 = [a.py(3 符号 2 调用), b.py(2 符号 1 调用)]。b.py 组失败 →
     commit_batch 返回 Err（含 injected db failure），断言整批回滚不残留：
     symbols/calls/file_instances/file_versions/file_contents 全部为 0。
  2. **幂等重放**：DROP TRIGGER 后同批重跑 → Ok；与「一次成功导入」的对照库逐表
     对账（symbols/calls/file_instances/file_contents/file_versions 行数一致）、
     符号名集合一致、2 个文件均为 parsed。
- 计数器为软状态：a.py apply 成功时 inserted 已推进（i==1），DB 权威状态已回滚——
  测试如实断言该既有行为。

## 验证（命令原文 + 关键输出）

```
cargo test --lib test_commit_batch_db_error_rolls_back_and_replays
→ test daemon::fs_handlers::tests::test_commit_batch_db_error_rolls_back_and_replays ... ok
→ test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 2146 filtered out; finished in 1.16s

cargo test --lib daemon::fs_handlers
→ test result: ok. 59 passed; 0 failed; 0 ignored; 0 measured; 2088 filtered out; finished in 6.04s
```

## 结论

- plan §Verification V5 的「DB 错误→tx.rollback() 整批回滚→重跑幂等重放」现有直接单测覆盖。
- 端到端「中途 kill→重跑→对账一致」仍留 tester 在 F2 整改单取证（与本单边界一致）。
- fs_handlers 模块全绿（59 passed / 0 failed），无回归。

Date: 2026-11-27 15:20 (UTC+8)
