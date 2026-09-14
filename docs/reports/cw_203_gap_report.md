# Call Warden 203 子任务代码缺口清单（最终版）

**日期：** 2026-08-26　**依据：** 回收站对象恢复 + cw DB 函数级比对 + master 源码核实

## 一、恢复动作摘要

| 来源 | 结果 |
|---|---|
| 回收站（`$Recycle.Bin`，非 COM 解析 `$I` 元数据） | **364 个 `.git/objects` 松散对象**（删除时间 08-25 15:17–15:19 = prune 窗口）→ 已恢复并并入主仓库 `.git/objects` |
| 恢复后的 git 对象 | **5 个 pilot commit + wip(6e650d9) + merge(5d3fd4b) 全部 materialize，tree 完整**；已建 `refs/recovery/pilot-*` 保护引用 |
| `~/.callwarden/git_backup_20260825/.git` | 8,513 对象，**0 个独有**（全在主库）→ 无新增价值，仅作一致性参考 |
| `C:/git_work/.git_bak_20260820` | 18 个独有 blob/tree，**0 个独有 commit** → 无价值 |
| 回收站 `.git.bak.20260825` | 0 个对象 |
| 5 个 worktree 磁盘 | 已备份 5.8G（`~/.callwarden/worktree_recovery_20260826/`），整文件二级恢复源 |

## 二、44 个 prune commit 的缺口判定（用户核心问题）

**数据口径：** `file_versions` is_current=1 共 113 个 distinct commit = **69 在 master + 44 已 prune**。
44 个 prune commit 涉及 **378 文件 / 7,879 函数（去重 7,300 个唯一函数）**。

### 文件级
- **338/378 文件在 master**（89.4%）
- **40 个不在 master** —— 全部为临时/实验文件：`_tmp_*.py`(24)、`g0-reviewer-scratch/*`(2)、`docs/_create_*.py`(5)、`_h4bc_matrix.py`、`_audit_p12_check.py`、`repro_link_attr.py`、`tests/_diag_mcp_call.py`、`i18n.py`(已确认 master 重构为 `callwarden/i18n/__init__.py` 包)

### 函数级（7,300 个唯一函数逐函数比对）
- 核心名（末段标识符）在 master 完全缺失的仅 **43 个**，逐一人工核实后：
  - cw_client.rs 13 个（QueryType/run_ping/…）→ **误报**（分词时误排 `bin/` 目录，master 实际 4~22 处）
  - `_tmp_*` 临时脚本 12 个 → 无价值
  - `_h_get_top_callers` 等旧助手 10 个 → **master 已重构**（新 `_h_*` 16 个：`_h_get_symbol_history` 等），且 MCP 工具等价物全在（top_callers 5、orphan_symbols 4、comment_coverage 4、call_heatmap 4、uncovered_functions 3）
  - audit_log 3 方法 + LazyDBProxy 2 + 测试 3 → **全部在 master**
- **结论：0 个真实代码缺口。** 44 个 prune commit 的内容（最终实现）全部已合入 master（同名或重构等价）。

## 三、203 子任务结论

- 5 个 pilot 任务（srv-001/002/003、cli-087/088）代表的可恢复代码：**全部在 master**（srv-002 的 db 修复在 master L4225；cli 测试文件在 master；srv-001 无独有源码）。
- 已处理的真缺口：srv-003 的 `backup_restore_handlers.rs`+`test_srv_003.py`（提交 901cc3c）+ `SRV-003_evidence.md`（已恢复落盘）。
- **无需整批重做 203 个任务**；遗留仅 40 个临时/实验文件（无生产价值，DB 中有函数文本可随时取）。

## 四、保护与台账

- 保护引用：`refs/recovery/pilot-srv-001/002/003`、`pilot-cli-087/088`
- 恢复对象库：`C:/Users/wanpi/.callwarden/objects_recovery/objects/`（364 对象，15MB，已并入主库）
- 台账：`cw_task_commit_ledger.json`（119 条 task↔commit 关联 + recovery_20260826 区块）
- 新纪律：每次完成任务追加 task_id↔commit_id 并刷新台账（损失隔离到单任务）

## 五、建议后续（可选）

1. 40 个临时文件如需归档，可从 DB `symbol_contents` 提取函数文本重建（无生产价值，一般不做）。
2. fsck 剩余 46 条 broken link（中间悬空 commit 碎片）→ 用户已选暂不 gc，不影响操作。
3. 若需 100% 干净：`git -c gc.reflogExpire=now -c gc.reflogExpireUnreachable=now gc --prune=now`（不可逆，恢复源已备份）。
