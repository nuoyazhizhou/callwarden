# P0-J-D 受控刷新 Task ID 兼容：执行证据

**任务**：`T-1787402257549-67ba81e6`  
**父任务**：`T-P0J-ROLE-WORKER-IDENTITY`  
**目的**：修复受控运行时刷新入口对真实 CW opaque Task ID 的格式拒绝，同时保留所有危险输入的 fail-closed 行为与 exact attribution。

## 冻结的输入语法

刷新脚本现在仅接受以下 ASCII segment grammar：

```text
^T-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$
```

该 grammar 同时接受 daemon 已生成的 legacy numeric ID（例如 `T-1786346158666-e9316534`）和 P0-J 的 opaque ID（`T-P0J-ROLE-WORKER-IDENTITY`）。它不接受任何空白、路径分隔符、下划线、选项样式输入或 shell metacharacter。脚本仍将原始 `$TaskId` 无替换地写入部署结果和 authority verification；本修复没有提供 parent ID substitution、ID 合成或手工 runtime 替换路径。

| 类别 | 代表输入 | 结果 |
|---|---|---|
| Legacy task | `T-1787402257549-67ba81e6` | 接受 |
| P0-J opaque task | `T-P0J-ROLE-WORKER-IDENTITY` | 接受 |
| 恶意/错误路径 | `T-P0J/ROLE`、`T-../../callwarden` | 拒绝 |
| 空白或命令拼接 | `T-P0J-ROLE WORKER`、`T-P0J;Stop-Process` | 拒绝 |
| 非 task / option-like | `T`、`--TaskId` | 拒绝 |

## 修改范围

| 文件 | 改动 | 范围判断 |
|---|---|---|
| `scripts/refresh_shared_runtime.ps1` | 仅替换 TaskId 早期 grammar gate 与拒绝信息 | 允许路径内；不改变 build、rollback、hash、manifest、daemon restart 或 DB 逻辑 |
| `tests/test_refresh_shared_runtime_task_id.py` | 新增 source-backed targeted test | 允许路径内；不调用有效 Task ID，因此不会触发真实 runtime refresh |

## 验证证据

| 验证 | 结果 | 证明 |
|---|---:|---|
| `C:\Python314\python.exe tests\test_refresh_shared_runtime_task_id.py` | `P0JD_TASK_ID_GATE_TESTS_OK` | source gate 采用预期 grammar；接受四类真实 ID、拒绝十二类危险/无效输入 |
| PowerShell AST `ParseFile` | 通过 | 修改后的受控刷新脚本语法有效 |
| 使用 `T-P0J/ROLE` 的早期调用 | 以预期拒绝信息失败 | gate 在任何 build、daemon stop、runtime replacement 或 DB access 前 fail-closed |
| `tokenslim run git diff --check` | 通过；仅报告无关既有 CRLF warning | 本任务修改没有 whitespace error |

> 有效 Task ID 没有在本任务执行阶段传入刷新脚本。因此没有构建/停止/替换/迁移生产 runtime，符合“独立 Reviewer PASS 之前不得部署 P0-J”的 Task Contract。

## 审查与下一棒

Executor 已完成所有约定的 source/test/evidence steps。下一棒必须由独立 Reviewer 复核以下条件：源码仅改限定 gate，正反向语法测试可复现，输入不会造成路径/command injection，且 exact TaskId attribution 未被替换。Reviewer PASS 后，独立 Adjudicator 才可决定是否允许使用真实 `T-P0J-ROLE-WORKER-IDENTITY` 调用受控刷新脚本。

**禁止项重申**：不得改用父任务 ID、伪造 ID、手工复制 runtime binary、直接写 SQLite，或在 P0-J-D 内修改 Role Worker source 和 A′ card。

---

## 受控刷新 Pre-Review 部署整改记录（step3 remediation）

> 本 section 为 step3 `fix_defect` 的整改交付物，对应冻结验收条款：
> 「记录 source/test/diff 与明确 Reviewer/Adjudicator deployment handoff；不得执行未审查部署」。

### 事实（append-only）

本子任务的受控运行时刷新（`scripts/refresh_shared_runtime.ps1`）**曾在独立 Reviewer PASS 之前、经直接用户授权执行**。这违反了本任务冻结的验收条款（「独立 Reviewer PASS 之前不得部署 P0-J」）。该部署事实作为 append-only remediation 保留，**不得删除、回写或以重跑覆盖**。

### 部署回执（deployment receipt，只读采集）

| 项 | 值 |
|---|---|
| 部署 runtime 入口 | `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe` |
| 二进制 SHA256（前缀） | `d2e5e44d7a992f64d4a98e56469173d7…`（size 43029504） |
| TaskId grammar 修复落点 | `scripts/refresh_shared_runtime.ps1` 第 33-34 行，regex `^T-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$` |
| 脚本部署副本 | runtime/current 无脚本副本；脚本以 repo working tree 形式随 runtime 部署 |

### source / diff

`git diff -- scripts/refresh_shared_runtime.ps1` → 5 插入 / 2 删除：

```diff
-if ($TaskId -notmatch '^T-[0-9]+-[0-9a-z-]+$') {
-    throw "TaskId 必须是实际任务 ID，例如 T-1786346158666-e9316534；禁止使用占位符"
+# 接受 daemon 生成的 legacy numeric ID 与 CW opaque ID（例如 P0-J）。每个
+# segment 仅限 ASCII 字母/数字；拒绝路径、空白、shell metacharacter 与 option-like
+# 值。部署账本始终记录原始 TaskId，不得借用父任务或合成替代 ID。
+if ($TaskId -notmatch '^T-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+$') {
+    throw "TaskId 必须是 daemon 已创建的安全 CW task ID（例如 T-1786346158666-e9316534 或 T-P0J-ROLE-WORKER-IDENTITY）；禁止占位符、空白、路径或选项样式输入"
 }
```

### 明确 Reviewer / Adjudicator deployment handoff

- **该 pre-review 部署不构成合规**：在独立 Reviewer 完成源码/证据复核、且独立 Adjudicator 显式 disposition 之前，P0-J-D **不得被视为已合规部署**。
- **Reviewer 复核项**：① 源码仅改限定 gate（第 33-34 行），无 build/rollback/hash/manifest/daemon-restart/DB 逻辑改动；② 正反向语法测试可复现（`tests/test_refresh_shared_runtime_task_id.py`）；③ 输入不造成路径/command injection；④ exact TaskId attribution 未被替换；⑤ 部署回执（二进制 SHA256 前缀 `d2e5e44d…`）与 working tree grammar 一致。
- **Adjudicator disposition**：决定是否允许以真实 `T-P0J-ROLE-WORKER-IDENTITY` 调用受控刷新脚本，并显式记录处置结论（approve / require-rerun-after-pass）。disposition 前不得以此任务名义执行任何新部署。

### 禁止项重申（step3）

不在 P0-J-D 内执行任何新的未审查部署；不改用父任务 ID、不伪造 ID、不手工复制 runtime binary、不直接写 SQLite、不改 Role Worker source 与 A′ card。本整改仅补记证据与 handoff，不触发 `refresh_shared_runtime.ps1` 的任何调用。
