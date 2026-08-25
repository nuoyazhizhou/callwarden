# Pilot：worktree 并行三角色执行 —— CLI-084/085/086

## 结论
**用户模型成立 ✅** —— 每任务一个 worktree（源码隔离），角色在 worktree 内串行（implementer→reviewer→adjudicator），跨任务并行。worktree 隔离实测有效；daemon 单实例但并发安全；唯一需补的轻量护栏（每 worktree `CARGO_TARGET_DIR` / 汇合 fence）在 pilot 中经 `target/` 天然隔离 + 串行 convergence 已满足。

## 范围
- 3 张卡：CLI-084/085/086（`cw local-next / local-reopen / local-report` → Rust daemon HTTP thin client）。
- 3 个 git worktree，clean `HEAD` 87ea1b6，分支 `pilot/cli-084|085|086`。

## 隔离验证（pilot 核心问题）
| 维度 | 结果 |
|------|------|
| 源码树 | 3 个独立 checkout；各写**不同文件名** `tests/test_cli_08X_http_rpc.py` + `deliverables/.../CLI-08X_evidence.md` → **零源码碰撞** |
| `cargo target/` | 天然 per-worktree（隔离，无共享 target 冲突） |
| daemon + DB | 单实例 127.0.0.1:12376；3 个 implementer subagent **并行**打它，5/5 检查全 PASS，无损坏 → **并发安全** |
| 角色串行 | 单任务内 implementer→reviewer→adjudicator 状态机强制串行；跨任务并行 |

## Implementer 阶段（并行）
3 个 subagent 各自在 worktree 内：
- 写 `fixture_negative_matrix` 测试（success / invalid / authority / unavailable / restart 负向矩阵，对线上 daemon 真实 HTTP transport）。
- 写证据清单 `CLI-08X_evidence.md`。
- 跑 5 检查（全 PASS），commit。
- commits：`b910f51`(084) / `3818a62`(085) / `be96878`(086)。

## Convergence 阶段（串行，共享 daemon）
每任务：executor `report` steps→`review` → 独立 reviewer `verdict.submit`(legacy overall=pass) → adjudicator `task.apply`(`applied`) → `task.close`(`closed`)。
| 卡 | verdict | 终态 |
|----|---------|------|
| CLI-084 | V-8c2696538072125a2cf11bde | closed |
| CLI-085 | V-39c119e36f30867776015ed7 | closed |
| CLI-086 | V-6f1d83c7503aea99e0bbb83f | closed |

## 发现（诚实记录）
1. **CLI-085 step0 target-symbol 漂移**：契约 target `cli_local_reopen_handlers.rs::handle_task_reopen` 在 HEAD **不存在**；但 `task.reopen` RPC 实测**可用**（返回 success）。属文档/契约符号漂移，非功能缺口 → 在 verdict 中以 info finding 记录（不阻断）。
2. 三命令 `_local_next/_local_reopen/_local_report` **早已**经 `route_task_write` 走 HTTP（主路径 HTTP-only，内层 `db.*` 仅离线 fallback）→ step1 `thin_cli_client` 验收「无直接 DB/Unix 路径」由既有代码满足，本 pilot 仅补测试+证据。
3. 真实新增代码是**独立测试文件**（不同文件名）→ 这正是 worktree 零碰撞的根因，也是放大到 26 张时热文件（dispatch.rs 等）需分组/merge 兜底的依据。

## 下一步：合并回主干（集成）
3 个 pilot 分支只新增文件，合入 master 干净。但**主 worktree 当前有 P0 未提交改动**（`task_collab.rs`/`cli/main.py` 等治理修复）。集成前先处理：
```
cd C:/git_work/callwarden
git stash                      # 暂存 P0 未提交改动
git merge --no-ff pilot/cli-084 pilot/cli-085 pilot/cli-086 -m "pilot: CLI-084/085/086 thin-client negative matrix"
git stash pop                 # 恢复 P0 改动
git worktree remove C:/git_work/cw-wt-084 C:/git_work/cw-wt-085 C:/git_work/cw-wt-086
```
（或先把 P0 改动单独 commit 再 merge，避免 stash 往返。）

## Epic 子树现状
closed 31 / in_progress 23 / review 133（pilot 将 3 张 in_progress→closed）。剩余 23 张 in_progress 可按同模式续推。
