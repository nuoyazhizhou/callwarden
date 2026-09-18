# Git 对象库清扫事故报告与恢复记录（2026-09-14 17:34–17:38 窗口）

> **恢复完成（2026-09-14 19:35）**：恢复提交 **`96fa721c34b24d5563c82ca84ea29796f7507368`**
> （父 = 完整锚点 `e064853`，树 = 2472 tracked 文件全量重建）。旧 tip
> `b6fbcc0` 保全于分支 `recovery/pre-sweep-20260914`。HEAD 可达对象 fsck 零缺失。

## 1. 事故概述

- **时间窗**：2026-09-14 17:34–17:38（回收站删除时间戳），恰逢本会话对
  `tests/_w3_harness.py` 等 3 文件执行 `git stash push` 与 pytest 运行期间。
- **现象**：`C:\git_work\callwarden\.git` 下大量内容被移入 Windows 回收站：
  - `.git/objects/` **全部 loose objects + 全部 .pack 文件**（仅剩 .idx）
  - `.git/refs/`（整个目录）、`.git/logs/HEAD`
  - `.git/index.lock`、`.git/index.stash.26988`、`.git/worktrees/*` 管理文件
- **波及范围（同窗其它仓库）**：`C:\git_work\TokenSlim\.git` 的 refs（2 文件）、
  `packed-refs.lock`、`index.lock`、`HEAD.lock`、`AUTO_MERGE.lock`。
  另：回收站显示 **09-13 当天已有一次更大规模同类清扫**（TokenSlim/.git/objects
  5349 个等，~46k 文件）——提示本机存在某个周期性/触发性清理进程，**根因未查明，
  可能复发**。
- **未受损**：callwarden 工作树全部文件（含卡 C/C-16、卡 D/C-17 提交的全部产物内容）、
  C-18 未提交改动、`.git/config`/`packed-refs`（陈旧但完整）。

## 2. 恢复过程（时间序）

1. 发现 `git status` 报 not a git repository → 定位 `.git/refs` 与 objects 缺失。
2. 备份 .git 元数据（HEAD/config/packed-refs/ORIG_HEAD/REBASE_HEAD/FETCH_HEAD/index）
   至 `C:\git_work\callwarden-metadata-backup-20260914\`。
3. 重建 `.git/refs/{heads,tags,remotes/origin}` 目录骨架（非破坏）。
4. 从回收站恢复：解析 `$I`（deletion timestamp + 原始路径）→ 写回 `$R` 内容。
   **匹配窗口 = 2026-09-14 17:00–18:00 且路径含 `\git_work\`**：
   - 匹配 1807、成功恢复 **1806**、目标已存在跳过 1、`$R` 缺失 222（内容已被清出
     回收站，不可恢复）、错误 0。
   - 恢复内容包括：2035 个 objects 中的 1813 个、refs（13，全部为 tags）、
     logs（12，含 master reflog）、worktrees 管理文件、**全部 .pack（含
     2026-09-14 12:54 的 10MB pack，覆盖 ≤12:54 全部历史对象）**。
5. 重建 `refs/heads/master`。经校验：
   - 恢复后**今日 6 个提交对象中 5 个完整**（5cfb158/480829e/ed84aa9/105a114/b6fbcc0），
     **186582e（卡 D fix 提交）对象缺失**（在 222 个不可恢复之列）。
   - `git fsck --full --connectivity-only`：自旧锚点可达人引用共 **148 个缺失对象**
     （集中于 09-14 12:54 pack 之后的提交树：b2056b4/78bb2af/5cfb158/480829e/ed84aa9/
     186582e 的 tree/blob，以及 09-13 已存在的旧缺口）。
6. **决策**：按本仓 2026-08-28 灾难恢复先例（`53b2958`「recovery: 对象库灾难恢复——
   重建 9855da6 之后全部本地工作」），采用**单笔恢复提交**：
   - 以最后一个完整提交 `e064853`（09-11，在 12:54 pack 内）为父；
   - 树 = 17:34 工作区 index 快照（2472 个 tracked 文件）的逐文件 blob 重建：
     blob 对象存在者沿用原 id；缺失者按「内容决定哈希」从工作树精确重建
     （工作树内容 == b6fbcc0 提交内容）；
   - **3 个 C-18 已编辑未提交文件**（tests/_w3_harness.py、
     tests/test_mcp_assignment_show_http_rpc.py、
     tests/test_mcp_compat_identity-lease-small_http_rpc.py）按 b6fbcc0 时点内容
     取 e064853 版本（C-18 编辑不在恢复提交内，随后以 C-18 卡自身 task_id 正常提交）；
   - 被清扫的 6 个旧提交哈希以分支 `recovery/pre-sweep-20260914` 与本文档永久留痕。

## 3. 旧→新哈希映射（被清扫提交链）

| 旧提交 | 内容 | 处置 |
|---|---|---|
| `e064853689695796528b4b556053658aeea296d6` | 09-11 docs（锚点，完整） | 保留为恢复提交父 |
| `78bb2af46e59da2ea33644fc28b2d4c6696b50d1` | C-14/15 governance | 对象存活、树缺失 → 并入恢复提交 |
| `b2056b48eb20f122303c628c31049fb6422097b1` | C-16/C-17 建卡回执 governance | 同上 |
| `5cfb1589f6df5a3844f8d515ae3f3bc49415e030` | C-16 fix（task_collab_lease.rs + 测试） | 同上 |
| `480829e33ee8f07fe7b69a3e281cf22df773c79e` | C-16 台账 | 同上 |
| `ed84aa9e69ae303e2de6056259e4427f580d481d` | C-16 reviewer/adj + §W18/§W19 | 同上 |
| `186582ee2f18dddae41500b4d3556eca267d9a0e` | C-17 fix（admin 路由 + 测试） | **对象缺失**，内容 = 恢复提交中的现工作树版本 |
| `105a114b4bea66ed879cfe289cf2e3b6f72056e8` | C-17 台账 | 对象存活、树缺失 → 并入恢复提交 |
| `b6fbcc091ae8334c9a4de37cd15adac416d1dc90` | C-17 governance（原 master tip） | 同上 |

## 4. 遗留风险与建议

1. **根因未查明**：17:34–17:38 清扫进程身份未知（回收站式删除，非 git 操作）；
   09-13 已有同类大规模清扫。建议用户检查计划任务/清理工具/杀软隔离区，
   并考虑对 `.git` 目录加排除规则。
2. **222 个对象不可恢复**：其中含 186582e 提交对象与若干旧树；其**内容**已全部
   由工作树/恢复提交保全，但原始哈希不可复现（涉事提交的 id 已在台账/daemon
   审计中固化，以本映射文档对照）。
3. **TokenSlim 受损未修**：其 .git/objects 在 09-13 清扫中大部丢失（本次仅恢复
   17h 窗口内其 2 个 refs 文件），需另行按同法恢复（回收站 09-13 窗口仍有
   ~46k 个 $I 可用）。
4. 恢复提交后建议尽快 `git gc` 前先做 bundle 备份（但注意本机清理进程风险）。
