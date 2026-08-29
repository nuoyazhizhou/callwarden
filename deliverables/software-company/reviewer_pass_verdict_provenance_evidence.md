# Reviewer PASS Verdict Provenance 修复证据

## Task binding

- task_id: `T-1788019804377-eb4595d8`
- implementation step: `S-1788019804378-eb5428b4`
- test step: `S-1788019804378-eb5591a4`
- deploy step: `S-1788019804378-eb55e53c`
- evidence step: `S-1788019804378-eb562740`
- implementation commit: `50928e3c66679d8b49c6fc6c9267abbeb2413b81`
- commit ledger commit: `738fcf9a904f90a5efd158f8f82067ab75f04cce`

## 修复内容

`task.handoff` 的 `reviewer_pass` 现在在写入 handoff ledger 前由 daemon 原子校验：

1. 同一 task 的真实 `pass` Verdict Ledger；
2. Verdict 的 `step_id` 与 handoff source step 一致；
3. `snapshot_id` 与 `view_manifest_hash` 非空；
4. Verdict workspace 与 task workspace binding 一致；
5. Verdict reviewer identity 的 agent/session/model/role 与当前 handoff identity 一致。

缺失或不匹配时返回结构化错误并不写入 handoff。有效 PASS 会把
`source_verdict_id`、`snapshot_id`、`view_manifest_hash` 写入 handoff envelope，且把 snapshot
写入 task event 的 projection 列，供 governance projection 使用。历史 task、verdict、evidence
和 assignment 事件未被改写。

## 回归验证

命令：

```text
cargo test --manifest-path rust_ext/Cargo.toml test_reviewer_pass_requires_task_bound_verdict_before_handoff --lib
```

结果：`1 passed; 0 failed`。

测试覆盖：无 Verdict Ledger 时 `E_HANDOFF_VERDICT_REQUIRED` 且 handoff 数量为零；补入同 task/step/
workspace、同 reviewer identity 且含 snapshot/manifest 的 pass verdict 后 handoff 成功，并验证
source verdict 与 snapshot provenance 被持久化。

## 官方 runtime

- refresh evidence: `C:\Users\wanpi\.callwarden\runtime\evidence\20260830-002135-738fcf9a904f-0e3d006c.json`
- refresh evidence sha256: `908B0C022E06EDEF7AA910511DB63D7AC2506A9D05301517D8C7F6A9E78CA66E`
- runtime_version: `20260830-002135-738fcf9a904f-0e3d006c`
- daemon PID: `52016`
- cw-daemon sha256: `1261751e11bb773ee463db816878cbd5508627539d4b1fe67da71831b9dce216`
- `cw.py daemon ping`: exit code `0`

## 当前目标任务的边界

Executor 未冒用 Reviewer identity，也未为任何真实任务生成 Verdict Ledger 或执行 apply/close。
当前目标任务 `T-1788011722055-1b59cb4c` 仍按 daemon 投影为 `review_pending`，因为它本身还没有
合法持久化 Reviewer verdict；该历史任务需要独立 Reviewer 通过 `verdict.submit` 后再 handoff。
