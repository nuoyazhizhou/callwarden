# Wave 3 · 治理写路径环 E2E 报告（2026-10-29）

## 结论

| 项 | 结果 |
| --- | --- |
| 目标 | 矩阵卡 `T-1793248351476-e7d6650c` 步骤 0：claim/report/verdict/apply/close 全链路 + reviewer lease 门禁 |
| 探针卡 | `T-1793249640949-22623714`（1 步，ws=1 / instance `4baea3ff12c2ea5c` / binding `tb-T-1793249640949-22623714-4baea3ff12c2ea5c`） |
| 代码基线 | `ee9a432`（owner ACL + legacy strict binding 门禁已部署） |
| review snapshot | `66ab3f4a48c0a7ed`（generation 7，snapshot.publish） |
| verdict | `V-d49b2e68c6b6729457e7b091`（event 805，`blind_first_pass`，overall=pass） |
| 判定 | **PASS**：全链路正向通过；5 条负向门禁全部按预期 fail-closed（含 legacy 无 binding 写面） |

## 正向链路（事件顺序）

| # | 动作 | 身份 | 观测结果 |
| --- | --- | --- | --- |
| 1 | `cw task next` claim step | `agent-ds4-bayes-exec` / implementer | `S-1793249640952-2294f744` → in_progress |
| 2 | `cw task report` | 同上 | `report_request_id=req-6716e9b64cf8`，状态进入 review |
| 3 | `cw collab publish` | daemon | `snapshot_id=66ab3f4a48c0a7ed`，generation 7 |
| 4 | `cw collab verdict` | `agent-ds4-t05-reviewer` / reviewer + lease `L-20cabfaa75bf8e33`(fencing 1) | `V-d49b2e68c6b6729457e7b091`，event 805 |
| 5 | `cw task apply` | `agent-ds4-t05-adj` / adjudicator + lease `L-ced4ff62f3fb9e99`(fencing 2) | applied @1793249751.19 |
| 6 | `cw task close` | 同上 | closed @1793249755.83 |

契约引用：task contract `TC-T-1793249640949-22623714` rev 1（`sha256:a410dd0d…`）；
reviewer role contract `rcr-T-1793249640949-22623714-reviewer-r1` rev 1（`sha256:3e8debc9…`）；
view manifest hash `sha256:6081e971…`（daemon prompt bundle context_hash）。

## 负向门禁（全部 fail-closed）

| # | 用例 | 命令 | 观测错误 |
| --- | --- | --- | --- |
| N1 | apply 缺 lease 凭证 | `cw task apply …`（无 `--lease-token/--fencing-counter`） | `E_LEASE_REQUIRED` |
| N2 | close 缺 lease 凭证 | `cw task close …`（无凭证） | `E_LEASE_REQUIRED` |
| N3 | 陈旧 fencing counter | `--fencing-counter 999`（当前 1） | `E_LEASE_FENCING_STALE: fencing counter 999 != 当前 1` |
| N4 | holder 与 lease 不一致 | 身份 `agent-ds4-t05-adj` + lease 持有者 `agent-ds4-t05-reviewer` | `E_LEASE_HOLDER_MISMATCH` |
| N5 | legacy 无 binding 写面 | `cw task reopen T-1787183083779-b9405964`（unbound closed） | `E_TASK_BINDING_ATTESTATION_REQUIRED` |
| N5' | 同上只读面 | `cw task show T-1787183083779-b9405964` | 正常返回（closed / qa-probe） |

补充正向证据（attestation 解锁写面）：`T-1784213740704-90c2` 已在 attestation 流程中补 binding，
因此 `task.reopen` 不再被 strict 门禁拒绝（返回 in_progress），验证完成后以 `cascade_close` 恢复 closed。

## 跨 uid ACL

- Windows 单用户下 `peer.uid` 恒为 `u32::MAX`，无法在真机区分 uid，故本矩阵不覆盖；
  跨 uid 语义由单测 `rust_ext/src/daemon/task_collab_tests_owner_acl.rs` 覆盖（4 个用例，
  bound 任务跨 uid → `workspace_forbidden`，legacy/missing registry 行放行，strict 写面要求 attestation）。
- Linux 多用户下的隔离点在 UDS `PeerCredential.uid` → `daemon_workspaces.owner_uid` 比对，实现见
  `task_collab.rs::verify_registry_owner_for_instance` 与 `dispatch.rs::check_task_owner(_strict)`。

## 复跑方式

```powershell
# 1) 建探针卡并 claim
cw task create --title "[E2E] probe" --desc "probe" --steps '[{"action":"probe","target_file":"deliverables/e2e-matrix-wave3.md"}]'
cw task next <probe_id> --agent-id agent-ds4-bayes-exec --agent-instance-id inst-ds4-bayes-exec --session-id sess-ds4-bayes-exec --model-id deepseek-v4.1-flash --role implementer

# 2) 负向：无凭证 apply/close（期望 E_LEASE_REQUIRED）
cw task apply <probe_id> --agent-id agent-ds4-t05-adj --agent-instance-id inst-ds4-t05-adj --session-id sess-ds4-t05-adj --model-id deepseek-v4.1-flash --role adjudicator
cw task close <probe_id> --agent-id agent-ds4-t05-adj --agent-instance-id inst-ds4-t05-adj --session-id sess-ds4-t05-adj --model-id deepseek-v4.1-flash --role adjudicator

# 3) 负向：始终 fencing / holder 不一致
cw lease acquire <probe_id> --role reviewer --agent-id agent-ds4-t05-reviewer --session-id sess-ds4-t05-reviewer --model-id deepseek-v4.1-flash --agent-instance-id inst-ds4-t05-reviewer --ttl 3600 --json
cw task apply <probe_id> … --lease-token <token> --fencing-counter 999      # E_LEASE_FENCING_STALE
cw task apply <probe_id> --agent-id agent-ds4-t05-adj … --lease-token <token> --fencing-counter 1  # E_LEASE_HOLDER_MISMATCH

# 4) 正向：report → publish → verdict → apply → close（adjudicator 需自持 lease）
cw task report <probe_id> <step_id> --result "…" --agent-id agent-ds4-bayes-exec …
cw collab publish --workspace c:\git_work\callwarden --json
cw collab verdict --task-id <probe_id> --step-id <step_id> … --snapshot-id <snapshot> --phase blind_first_pass --overall pass --attestation "…" --agent-id agent-ds4-t05-reviewer …
cw lease release <probe_id> --role reviewer --token <reviewer token> --agent-id agent-ds4-t05-reviewer …
cw lease acquire <probe_id> --role reviewer --agent-id agent-ds4-t05-adj … --json
cw task apply <probe_id> … --lease-token <adj token> --fencing-counter <new counter>
cw task close <probe_id> … --lease-token <adj token> --fencing-counter <new counter>

# 5) legacy 门禁
cw task reopen <unbound_closed_id> --reason "…" --agent-id agent-ds4-t05-adj …
```

## backlog（不阻塞本步）

- Wave 4：并发/崩溃/多实例（并发部署、lease 抢占、daemon 重启恢复）
- lease 过期路径（`E_LEASE_EXPIRED`）需等待 ttl 自然到期，本轮以 fencing/holder/缺凭证覆盖
- 跨 uid 真机验证需 Linux UDS 环境
