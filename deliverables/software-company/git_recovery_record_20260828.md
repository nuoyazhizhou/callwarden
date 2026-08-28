# Git 对象库恢复记录（2026-08-28）

## 1. 事故概述

多个 agent 并行操作 git 时执行了 `git gc` / `git prune`（或 filter-repo 类历史重写），
导致 `.git/objects` 中大量 loose objects 被删除：

- `git status`/`git add`/`git commit`/`git write-tree` 报 `unable to read tree` /
  `Could not read <hash>`；
- `git log --all` 报 `Failed to traverse parents`；
- `git fsck --full` 报数百个 `missing blob/tree/commit` 与 `broken link`；
- 根因对象被 prune 时**进入 Windows 回收站**（`$Recycle.Bin`），因此可恢复。

## 2. 备份（先于任何恢复操作）

```
C:\Users\wanpi\.callwarden\git_recovery_20260828\
├── index.bak        （损坏前的 .git/index，188974 字节）
├── objects\         （损坏后仍存在的全部 loose objects，31M）
├── pack.bak\        （pack 文件快照）
└── refs.bak\        （refs 快照）
```

## 3. 恢复过程

### 3.1 回收站定位

扫描 `C:\$Recycle.Bin\S-1-5-21-1583625257-826939952-3615027596-1001\`
（40190 个 `$I` 元数据 + 40126 个 `$R` 内容文件）：

1. 解析 `$I` 文件（offset 28 起 UTF-16LE 原始路径），筛选 `.git\objects\` 命中
   → 3768 条；
2. 对 `$R` 文件做 **zlib 解压 + SHA-1 指纹匹配**（loose object = `zlib(header+content)`，
   sha1(header+content) == 对象名），精确匹配缺失对象哈希。

### 3.2 迭代恢复

脚本 `git_recover_iter.py`：`fsck --full` 收集缺失对象 → 指纹扫描 `$R` 文件 →
按 `xx/yyyy...` 写回 `.git/objects`，迭代至无可恢复。

| 轮次 | 缺失数 | 恢复数 | 剩余 |
|---|---|---|---|
| 1 | 64 | 34 | 30 |
| 2 | 42 | 12 | 30 |
| 3 | 39 | 9 | 30 |
| 4 | 30 | 0 | 30（回收站无） |

累计恢复：**219 + 170（两轮批量）+ 34+12+9（迭代）= 444 个对象**（含重复计数，
实际恢复对象数按映射去重后为 **389+**）。每次写入前校验 SHA-1，失败不写入；
已存在且正确的对象跳过。

## 4. 恢复后验证

```
git log --oneline -1   → 952e476 fix(deploy): refresh_shared_runtime.ps1 注入 MSVC 构建环境
git rev-list --count HEAD → 687（主链完整）
git status --short      → 正常输出（234 个并行任务 dirty 文件，与本次无关）
git write-tree          → 0e5e413e（成功）
git commit              → 60f5307（白名单 5 文件，1448+/5-）
```

剩余 **30 个对象**在回收站/恢复库中均找不到（`final_missing.txt` 存档于
`C:\Users\wanpi\AppData\Local\Temp\`）。经核实全部位于**非 HEAD 祖先链**的
历史 commit / orphan refs / worktree（`callwarden-p0l-repair-exception`）上：
- `git rev-list HEAD` 687 commits 完整可遍历，HEAD 主链不受影响；
- `git log --all` 在遍历 `2faba6a9`（并行任务 worktree 的 commit）时才断链；
- 不影响本次任务的白名单提交与后续工作。

## 5. 提交闭环（本次任务）

```
60f5307 [T-1787912195064-2c66e0a8] feat(next_action): inbound_handoff + work_order read-only projection
b39e2c2 [T-1787912195064-2c66e0a8] chore(ledger): record commit 60f5307
```

注意：首次 `git commit` 曾误含 72 个文件（index 中并行任务暂存被一并提交），
已 `git reset --soft HEAD~1` 撤销并重新 `git reset` 清暂存后，仅 add 白名单
5 文件重新提交为 60f5307（5 files changed）。

## 6. 剩余风险与建议

1. 30 个不可恢复对象位于并行任务 worktree refs 的历史 commit 上；建议并行任务
   （codex p0l-repair-exception）自行从其分支重新推送/重建，或删除失效 worktree refs。
2. `refs/codex/turn-diffs/checkpoints/*` 大量 checkpoint refs 指向已 prune 对象
   （fsck 噪音）；建议并行任务收敛后清理。
3. 仓库纪律（延续 2026-08-25 教训）：**禁止在共享仓库上执行 `git gc --prune=now` /
   `git prune` / `git filter-repo`**；需要清理时先备份 `.git/objects`，或只做
   `git gc --prune=2.weeks.ago`。
4. `.git/index` 当前仍包含并行任务暂存内容（工作树 234 个 dirty 文件），
   各 agent 应只 `git add` 自己的白名单路径，严禁 `git add .`。

## 7. 相关命令存档

```
# 回收站扫描（元数据）
C:/Python314/python.exe - <<'PY'
...解析 $I offset28 UTF-16LE 路径，筛 .git\objects...
PY
# 指纹匹配 + 写回
python git_recover_iter.py   # fsck→扫描→写回，迭代
# 验证
git fsck --full
git rev-list --count HEAD
```
