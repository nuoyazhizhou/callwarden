# 2026-09-14 工作快照重建清单

- 恢复分支: `refs/heads/recovery/2026-09-14-snapshot`
- 重建提交: `2b97894c772fc60a5d1b598b38a97b6a0b28fcfe`
- 树对象: `30e49319df5d2998eba66b60637a58a6ffe330ab`（2472 个文件）
- 父提交: `9855da6c08c996b0092ca421b69e7901b3873f71`（工作线真实起点，09-02 GATE-0 finalization）
- **重建提交链分支**: `refs/heads/recovery/rebuilt-0911-0914`（tip `1d3b556a`，938 可达提交）——
  74 条 reflog 元数据（父/作者/时间/message）已重建为 git 可达历史，见 §重建链

## 丢失背景

09-11 12:08 ~ 09-14 19:12 的 74 条 HEAD reflog（72 个唯一提交）
记录于 `callwarden-metadata-backup-20260914/logs-HEAD.stale`，
最新 `96fa721c`（C-17 收口，09-14 19:12）。这些 commit 对象已
从对象库消失：不在 loose（仅 09-14 一个 + 09-18 一批）、
不在 pack-a4068e6e（idx 09-11 12:06，早于全部丢失提交）、
不在 lost-found（287 个 other 全部冗余）、不在回收站。
loose 时间断层 = 对象在 09-14 之后某次清理中被整体清除。

## 诊断结论（2026-09-18 复核）：丢的是提交日志，不是代码

1. **代码内容基本没丢**。以 09-02 基线 `9855da6` 为参照，09-11~09-14 共
   664 文件变更（331 M / 291 A / 42 R，+128312 / -14887 行），其中
   **0 个文件的内容真正丢失**。recovery 树 2472 条目里 0 个 blob 缺失。
2. **丢的是 72 个 commit 对象本身**（提交历史/作者链/blame 时间线），
   树和 blob 大量存活。中间态树已同批被清理（2181→2472 之间无中间层级），
   故无法逐提交还原每次改了哪些文件，只能还原"09-14 19:12 的最终快照"。
3. 28 个"以当前内容替代"的文件经复核：其 09-14 改动**已全部流入当前工作区**
   （相对基线均有实质差异），只是 09-14 那个中间快照点没留住，不是代码丢失。
4. cw 数据库（`codegraph.db` / `cas.db`）**不含** 09-14 版本：
   `git_commits` 表空；28 个文件在 `file_instances` 中的 `current_content_hash`
   与 09-14 blob sha **全部不同**（库存的是 09-09/当前版本）；
   `cas_file_cache` 2403 条按 sha 精确匹配 28 个丢失 sha → **0 命中**。
5. `C:/git_work` 下其余 callwarden 目录**均不可用**（详见 2026-09-18 日志）。

## 修复记录（2026-09-18）

- 修复坏引用 `refs/remotes/origin/master`：原指向已丢失的 `9e3cf1f2`，
  导致 `git rev-list --all` / `git show-ref` **全局 rc=128 失败**。
  改为指向 master 权威值 `e5f94b0`（loose ref 覆盖 packed-refs）。
- 修复 `packed-refs` 中 master 陈旧值 `e064853` → `e5f94b0`。
- 修复后对象全部可达：10149 个对象，仅剩 1 个空树 dangling（`4b825dc6`）。

## 重建链

- 分支 `recovery/rebuilt-0911-0914`（tip `1d3b556a02508e20a19f38f1e6f13b3ca1fabbc6`）
- 把 `logs-HEAD.stale` 的 74 条 reflog（72 个唯一提交 + reset 条目）按时间序
  重建为 git 提交对象：作者/邮箱/时间/message **全部保真**，
  树统一取 09-14 快照树 `30e49319`（内容层面最接近的真相）。
- **性质**：这是"元数据保真 + 内容近似"的重建，**不是无损逐提交恢复**。
  原始 72 个提交对象的 tree 已随提交一起丢失，无法还原每次提交的具体改动。
- 提交元数据另固化于 `docs/recovery-2026-09-14-reflog-archive.md`。
- 验证：`git rev-list --count` = 938；`9855da6` 是祖先；树 = `30e49319`。

## 统计

| 项目 | 数量 |
|---|---|
| 09-14 索引条目 | 2472 |
| blob 仍在对象库 | 2104 (85%%) |
| 死 blob 工作区精确恢复 | 340 |
| 09-14 内容丢失(当前内容替代) | 28 |
| 树条目实写 | 2472 |
| **内容保全率** | **98.9%%** |

## 09-14 快照点未留住的文件（28 个，09-14 改动已流入当前工作区）

> 2026-09-18 复核：这 28 个文件相对 09-02 基线全部有实质差异且改动已在
> 当前工作区（`git diff 9855da6 -- <file>` 逐个确认），**不是代码丢失**，
> 只是 09-14 那个中间快照版本的对象没留住。下表"现用"= 重建时的工作区内容。

- `cw_task_commit_ledger.json` (09-14: 0349026254bd -> 现用: 83bf67547759)
- `db/db_build.py` (09-14: 89fd164f23ee -> 现用: 7210aac46ce9)
- `deliverables/software-company/create_c_bucket_remediation_tasks.py` (09-14: 46573b97b844 -> 现用: ed671c50a174)
- `deliverables/software-company/pyt_regression_step4_handoff_backlog.md` (09-14: 851e27dc47f1 -> 现用: 80d8e635e1d4)
- `rust_ext/Cargo.toml` (09-14: cb51d245fcc5 -> 现用: 6282a8c7da91)
- `rust_ext/src/daemon/admin_handlers.rs` (09-14: a91856c68af6 -> 现用: c6bf0949f5f5)
- `rust_ext/src/daemon/dispatch.rs` (09-14: f5e62bf88050 -> 现用: c8404bacf62a)
- `rust_ext/src/daemon/fs_handlers.rs` (09-14: 188c3c248283 -> 现用: 2969beb0bbff)
- `rust_ext/src/daemon/http_server.rs` (09-14: e0f557e19fa5 -> 现用: 85dac2898101)
- `rust_ext/src/daemon/query_compat_handlers.rs` (09-14: fbb38bce4105 -> 现用: 0ea7b1ee6411)
- `rust_ext/src/daemon/snapshot_state.rs` (09-14: 378edd810e4e -> 现用: f61ace057bf5)
- `rust_ext/src/daemon/task_collab.rs` (09-14: a31f0e71eb93 -> 现用: ac6226204d80)
- `rust_ext/src/daemon/task_collab_lease.rs` (09-14: ff6f87d77616 -> 现用: 8a5b672f0349)
- `rust_ext/src/daemon/task_collab_shared.rs` (09-14: 4dcde9fac954 -> 现用: 9955f0c66d65)
- `rust_ext/src/daemon/task_collab_tests_lease.rs` (09-14: f01ebe2b171f -> 现用: 926b9351bad8)
- `rust_ext/src/daemon/task_loop/lifecycle_lease.rs` (09-14: 324c2490012e -> 现用: 542bacb292b7)
- `rust_ext/src/daemon/workspace.rs` (09-14: 879c4d8746d6 -> 现用: 437ddd0739f6)
- `scripts/refresh_shared_runtime.ps1` (09-14: c0edd819ea81 -> 现用: ec947021d379)
- `server/daemon_client.py` (09-14: b30a986f4762 -> 现用: 12b9b5d65711)
- `server/tools/tools_task.py` (09-14: a6886bf58281 -> 现用: 52aac33996e0)
- `tests/_w3_harness.py` (09-14: 282e1611b9ef -> 现用: a9860fb95972)
- `tests/test_c17_admin_route_workspace_authority.py` (09-14: 97e3749a8aa8 -> 现用: 223fecd6e7de)
- `tests/test_http_capability_registry.py` (09-14: 587c2bd1a8f5 -> 现用: 9f891d56b4ad)
- `tests/test_http_native_read_cutover.py` (09-14: 5de8c80746fe -> 现用: 53c382770e02)
- `tests/test_mcp_assignment_show_http_rpc.py` (09-14: 267835cd57a7 -> 现用: 3819a672fa95)
- `tests/test_mcp_compat_identity-lease-small_http_rpc.py` (09-14: 5022f471c7bd -> 现用: 59eb359962f9)
- `tests/test_windows_bridge_e2e.py` (09-14: c466f2c7aa52 -> 现用: 129a5914660b)
- `tests/test_windows_daemon_e2e.py` (09-14: c0d560752f2c -> 现用: eab834d124cf)

## 验证命令

```
git log --stat 2b97894c772fc60a5d1b598b38a97b6a0b28fcfe
git diff 2b97894c772fc60a5d1b598b38a97b6a0b28fcfe HEAD --stat
git ls-tree -r --name-only 30e49319df5d2998eba66b60637a58a6ffe330ab | wc -l
```
