# 证据清单（evidence manifest）— T5 capability 聚合接线

- **Task**: T-1795663552556-ba42830c（T5 capability 接线，REQ-PREQ-03 / AC-11）
- **Role**: executor（郑兑现，自证——T5 无独立 tester 取证，reviewer 可按命令原文复现）
- **身份**: t5-exec-408070ec / inst-t5-exec-408070ec / sess-t5-exec-408070ec / hunyuan-hy / role=executor
- **取证时间**: 2026-11-27 10:19–10:38（实现+定向测试）；2026-11-28 09:09（证据落盘）
- **对象**: worktree `C:/git_work/callwarden-worker/runs/impl-t5-capability-408070ec`，分支 `impl/t5-capability`，HEAD `40a21d4c184b7d45f28ee5f83c53112c7d73f31b`（= origin/master）；未提交改动仅 `dispatch.rs`（+48/-0）

## 一、证据文件清单与 sha256

| 文件 | sha256 | 说明 |
|---|---|---|
| `T-1795663552556-ba42830c-AC-11.md` | 277fe67bbbfb5df8e45de9c598bc9db751213f06345d86c6ee170a63a60260e3 | AC-11 八段证据（PASS，含缺口登记） |
| `T-1795663552556-ba42830c-capability-test.log` | e32e66f4f5c2945171e9d79be735e618ede4fe33886590e1e77b02ef2e3df5bd | `cargo test capability --lib` 原始日志（1323 行，line 1321 结论 16 passed / 0 failed，line 1323 CARGO_RC=0） |
| `T-1795663552556-ba42830c-capability-tests-keylines.txt` | 98816aeb3046c43e7fdee3f37cda43d5c0424cbe12c02adc437b3abaf3b1804d | 16 个 capability 匹配测试逐条 ok 行 + 结论行副本 |
| `T-1795663552556-ba42830c-fullrun-truncated-note.txt` | 8539cc662fc6293cb907ef753e70c50435dcf44e818b61b49db37225608016c6 | 全量回归截断件如实登记（1878 ok / 0 FAILED / 无结论行，不声称全量通过） |

## 二、环境指纹

- OS：Microsoft Windows 10.0.26300.9974（x86_64）；Git Bash（MINGW64）3.6.9
- rustc 1.93.1 (01f6ddf75 2026-02-11)；cargo 1.93.1 (083ac5135 2025-12-15)
- MSVC link.exe 14.44.35207（Hostx64/x64）
- Python 3.14.3（PYO3_PYTHON=C:\Python314\python.exe）
- 隔离构建：CARGO_TARGET_DIR=C:/Users/wanpi/AppData/Local/Temp/cw_t4_target（与 T4 共用热缓存）；PATH 前置 MSVC link.exe、剥 /usr/bin、保留 ~/.cargo/bin、补 /c/Python314
- 源文件 sha256（改动后运行时状态）：dispatch.rs=aa3f1b7fc508267ae729ab8bbec8922ed3c12392b0168a856c312831736cffb3
- 基线（HEAD 40a21d4）无 dispatch.rs 改动；worktree 仅 1 个 M 文件

## 三、命令原文

```bash
cd /c/git_work/callwarden-worker/runs/impl-t5-capability-408070ec/rust_ext
STRIPPED=$(echo "$PATH" | tr ':' '\n' | grep -v '^/usr/bin$' | tr '\n' ':')
export PATH="/c/Program Files/Microsoft Visual Studio/2022/Community/VC/Tools/MSVC/14.44.35207/Hostx64/x64:/c/Users/wanpi/.cargo/bin:/c/Python314:$STRIPPED"
export CARGO_TARGET_DIR="C:/Users/wanpi/AppData/Local/Temp/cw_t4_target"
export PYO3_PYTHON="C:\\Python314\\python.exe"
cargo test capability --lib > /c/git_work/callwarden-worker/runs/impl-t5-capability-408070ec/.t5_test.log 2>&1
echo "CARGO_RC=$?" >> /c/git_work/callwarden-worker/runs/impl-t5-capability-408070ec/.t5_test.log
```

## 四、统计

| 项 | 值 |
|---|---|
| collected（匹配 "capability"） | 16 |
| passed | 16 |
| failed | 0 |
| ignored | 0 |
| filtered out | 2136 |
| executed | 16 |
| rc | CARGO_RC=0 |

## 五、AC-11 判定

| 要求 | 判定 | 载体 |
|---|---|---|
| 三键齐 {name: prerequisite_gate_v1, schema_version: prerequisite_capability_v1, enabled: true} | **PASS** | capability_health_projection_exposes_prerequisite_gate_v1 |
| 聚合冒烟（health 投影含 prerequisites 键） | **PASS** | 同上 |
| role_prompt_compiler_v1 回归 | **PASS** | capability_health_aggregate_keeps_role_prompt_compiler_v1 + task_prompt_rpc_capability_projection_fields |

无 N/A 项，无 FAIL 项。

## 六、缺口与例外

1. **全量回归截断**：`cargo test --lib` 1878 条 ok / 0 FAILED / 0 真实 panic，但无结论行（会话中断），见 fullrun-truncated-note；不声称全量通过。AC-11 验收口径为 `cargo test capability`，证据完整。
2. **实现先于报告**：实现+定向测试 2026-11-27 10:19–10:38 完成；治理链 2026-11-28 补齐；期间未做任何 daemon 写操作。
3. **lease 接管**：昨日中断残留过期 lease L-fb0f9b12d1ccf84d，由同一身份原子接管（L-fbd004f110ec34dd，fencing=2）。
4. **行号漂移**：卡面 :450 vs 实际 handle_health 内 :448-451，按符号锚处理。
