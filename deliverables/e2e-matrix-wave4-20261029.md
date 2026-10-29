# Wave 4 · 并发 / 崩溃 / 多实例 E2E 报告（2026-10-29）

## 结论

| 项 | 结果 |
| --- | --- |
| 目标 | 矩阵卡 `T-1793248351476-e7d6650c` 步骤 1：并发/崩溃/多实例场景（并发部署、lease 抢占、daemon 重启恢复） |
| 探针卡 | `T-1793249969417-9ca03ca0`（1 步，ws=1 / instance `4baea3ff12c2ea5c`） |
| 代码基线 | daemon PID 20536 / `git_commit ee9a432`（owner ACL + legacy strict 门禁版本） |
| 判定 | **PASS**：停机窗口 fail-closed、重启后 lease/状态恢复、多实例单例守卫、lease 抢占 fencing 均符合预期；部署脚本并发锁缺失记为 finding F-1（不阻塞）。 |

## 场景 1：lease 抢占（同 task+role 重复 acquire）

| # | 动作 | 观测 |
| --- | --- | --- |
| 1 | `cw lease acquire` @ `agent-ds4-t05-reviewer` | `L-a92945ed92dfafd6`，fencing **1** |
| 2 | `cw lease acquire` @ `agent-ds4-t04-review`（未 release） | **成功**，`L-bbee6d5fa84a341d`，fencing **2**；旧 lease 不再 active |
| 3 | 旧持有者用旧 token + counter 1 执行 `task.apply` | `E_LEASE_TOKEN_MISMATCH`（active 行已切到新持有者，旧凭证失效） |
| 4 | `cw lease status` | `agent-ds4-t04-review` / fencing 2 |

结论：抢占通过 fencing counter 单调递增实现，**旧持有者写入被拒绝**（不会双写）。
与设计文档中 `E_LEASE_ACTIVE_EXISTS` 的差异：本实现允许"活跃 lease 下再 acquire 即抢占"，
未强制先 release（见 finding F-2，建议明确文档语义，不属缺陷）。

## 场景 2：daemon 停机窗口（崩溃可用性）

| # | 动作 | 观测 |
| --- | --- | --- |
| 1 | `Stop-Process -Id 20536` | 进程退出 |
| 2 | `cw daemon health` | `E_HTTP_MANIFEST_STALE: manifest PID 20536 已不存活（stale manifest）` |
| 3 | `cw task show …` | 同上 fail-closed，**未回退本地 SQLite** |
| 4 | `cw lease status …` | 同上 fail-closed |

结论：daemon 不可用期间治理读写一律 fail-closed（无静默降级）。

## 场景 3：daemon 重启恢复

| 项 | 重启前 | 重启后 |
| --- | --- | --- |
| PID | 20536 | 40744 |
| endpoint | `http://127.0.0.1:6374` | `http://127.0.0.1:14002`（**端口变化**，见 finding F-3） |
| lease | `L-bbee6d5fa84a341d` active / fencing 2 | 相同 lease、相同 token_hash、fencing 2（保持） |
| 任务状态 | review | review（保持） |
| snapshot 缓存 | generation 7 / `66ab3f4a48c0a7ed`（前序探针） | generation 1 / `d647215e8e04b67d`（缓存重建） |
| 后续链路 | — | publish → verdict `V-542331cc00cd425a291084a9`(event 806) → apply(adjudicator lease fencing 3) → close 全部成功 |

结论：lease 行与任务状态跨重启持久；snapshot 缓存按预期重建；重启后完整治理写链路可用。

## 场景 4：多实例 / 并发部署

| # | 动作 | 观测 |
| --- | --- | --- |
| 1 | daemon 运行中执行 `cw daemon start` | `{"status":"already_running","endpoint":...}`，未产生第二个进程 |
| 2 | 部署脚本 `scripts/refresh_shared_runtime.ps1` 并发保护 | 脚本内**无文件锁/mutex**；仅 AGENTS.md 约定"不可并发执行"，靠人工串行；内置逻辑会停止其他 owned daemon 后启动新的（串行时安全） |

findings：

- **F-1（medium）并发部署无强制锁**：两个 refresh 同时执行会互相回滚/覆盖 `current`。
  建议：脚本启动时创建独占锁文件（`FileShare.None`）并 fail-closed。本卡不阻塞。
- **F-2（low）lease 抢占语义与错误码文档不一致**：实现允许活跃 lease 被再次 acquire 抢占
  （旧持有者得到 `E_LEASE_TOKEN_MISMATCH`），`E_LEASE_ACTIVE_EXISTS` 在真机未触发；
  建议在 lease 设计文档中写明"抢占即重新签发 + fencing 递增"。
- **F-3（low）重启后 endpoint 端口变化**：daemon 未固定端口（6374 → 14002），客户端必须读 manifest；
  若存在缓存 endpoint 的调用方会短时不可用。建议在文档中标注 endpoint 非稳定标识。

## 复跑方式

```powershell
# 1) 探针卡
cw task create --title "[E2E] Wave 4 probe" --desc "probe" --steps '[{"action":"probe","target_file":"deliverables/e2e-matrix-wave4.md"}]'
cw task next <probe_id> --agent-id agent-ds4-bayes-exec … --role implementer
cw task report <probe_id> <step_id> --result "…" --agent-id agent-ds4-bayes-exec …

# 2) lease 抢占
cw lease acquire <probe_id> --role reviewer --agent-id agent-ds4-t05-reviewer … --json   # fencing 1
cw lease acquire <probe_id> --role reviewer --agent-id agent-ds4-t04-review … --json     # fencing 2（抢占）
cw task apply <probe_id> … --lease-token <旧 token> --fencing-counter 1                  # E_LEASE_TOKEN_MISMATCH

# 3) 停机窗口（fail-closed）
Stop-Process -Id <daemon_pid>
cw task show <probe_id> --flat                                                             # E_HTTP_MANIFEST_STALE
cw daemon health                                                                           # E_HTTP_MANIFEST_STALE

# 4) 重启恢复
cw daemon start
cw daemon health                                                                           # 新 PID
cw lease status <probe_id> --role reviewer                                                 # fencing 保持
cw collab publish --workspace c:\git_work\callwarden --json
cw collab verdict … --agent-id agent-ds4-t04-review --lease-token <B token> --fencing-counter 2
cw lease release <probe_id> --role reviewer --token <B token> --agent-id agent-ds4-t04-review …
cw lease acquire <probe_id> --role reviewer --agent-id agent-ds4-t05-adj … --json          # fencing 3
cw task apply <probe_id> … --lease-token <adj token> --fencing-counter 3
cw task close <probe_id> … --lease-token <adj token> --fencing-counter 3

# 5) 多实例
cw daemon start                                                                             # already_running
```

## backlog（不阻塞本步）

- 并发部署互斥锁（F-1）
- daemon 固定端口或 endpoint 抖动容忍（F-3）
- orphan lease 回收真机验证（需 holder 注册失效/心跳超阈值，下一轮可在隔离库模拟）
- 跨 uid 真机并发（需 Linux UDS 环境）
