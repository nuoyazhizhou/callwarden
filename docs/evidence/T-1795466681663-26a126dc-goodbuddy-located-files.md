# 证据 T-1795466681663-26a126dc · 步骤 S-1795466681671-2715d658：goodbuddy 关键实现文件定位

- 任务：T-1795466681663-26a126dc「深读 goodbuddy：Electron 桌面 agent 实现」
- 步骤：S-1795466681671-2715d658（step_index 0，action=定位关键实现文件（3-5 个））
- 认领身份：agent_id=callwarden-exec-worker-7f55，role=executor，session=callwarden-exec-worker-7f55-sess（daemon claim 响应 assignment_id=A-0d20275539010a7937f959d2）
- 分析对象：`c:/git_work/callwarden/testcode/goodbuddy`，HEAD `d6e44a68d9bccd74f39e624601bac89ea7809bd7`（"Merge remote-tracking branch 'origin/main'"）
- run 目录：`c:\git_work\callwarden-worker\runs\run-20261124-141559-7f55`（worktree，origin/master @ 6363e5b）
- 取证方式：直接源码 Read/Grep（本任务目标仓 goodbuddy 走图谱分析但仅到结构 + README 级；本轮深读为源码级定位，所有行锚点均在源目录实际 Read/Grep 核验）

## 一、候选方向 → 文件映射（步骤检查项 1）

| 候选方向（任务卡给定） | 命中文件 | 证据形态 |
|---|---|---|
| Electron 主/渲染进程分工 | `src/main/index.ts`（主进程启动编排）、`src/main/window.ts`（BrowserWindow webPreferences 安全配置）、`src/preload/index.ts`（contextBridge 单向 API 暴露）、`src/shared/ipc-channels.ts`（主-渲染共享 IPC 通道契约） | 已 Read/Grep 核验行锚点 |
| agent 会话管理 | `src/main/agent/runtime-session-binding-store.ts`（运行时会话绑定 + ACP session 原子认领 + 状态机监护）、`src/agent-daemon/daemon.ts`（agent daemon 主循环） | 已 Read/Grep 核验行锚点 |
| 桌面分发 | `build/agent-bundle.cjs`（锁定 runtime + 签名校验 + 完整性 manifest 的 agent bundle 构建/校验）、`electron.vite.config.ts`（主/渲染/preload 三入口打包配置） | 已 Read/Grep 核验行锚点 |

## 二、选定的 5 个核心实现文件（步骤检查项 2）

1. **`src/main/index.ts`（1912 行）— Electron 主进程启动编排核心**。import 面即分工全景：Electron `app/BrowserWindow/session/safeStorage/utilityProcess`（L7-19）、`ipcChannels`（L25）、`createMainWindow`（L68-72）、`registerIpcHandlers`（L45-48）。关键机制行锚点：`app.whenReady()` 启动链 L596；`session.defaultSession.setPermissionRequestHandler` / `setPermissionCheckHandler` L599-616（仅 mainWindow.webContents 放行的权限门）；`safeStorage` 加解密 L675-684（`isEncryptionAvailable` + `getSelectedStorageBackend` 判定，encrypt/decrypt L683-684）；`utilityProcess.fork` 拉起独立工具进程 L441/L481/L494；`mainWindow.webContents.send(ipcChannels.*)` 主→渲染推事件 L549/L558/L623/L1131-1132/L1561-1564；`registerIpcHandlers(...)` 挂载渲染→主请求 L1593，前置 `startupMark('main:ipc-register:start')` L1592。
2. **`src/preload/index.ts`（2114+ 行）— 渲染侧安全 API 桥（主/渲染进程边界的唯一出口）**。`contextBridge` / `ipcRenderer` / `webUtils` import L1；`ipcRenderer.invoke(ipcChannels.*)` 请求-响应式调用 L228-331（runtime native client L228-230、storage upgrade L232-234、app 窗口控制 L238-255、agentRun L331）；`ipcRenderer.on` + `removeListener` 返回退订函数的事件订阅形态 L261-271/L307-314。全量 API 通过 **`contextBridge.exposeInMainWorld('goodbuddy', desktopApi)` L2114** 暴露——渲染进程拿不到 ipcRenderer 本体，只能拿到白名单方法。
3. **`src/shared/ipc-channels.ts`（453 行）— 主/渲染共享 IPC 通道契约**。`export const ipcChannels` L1 单一通道名注册表；agent 相关 L69-74（`agentStatus`/`agentRun`/`agentCancel`/`agentQuestionRespond`/`agentCompactConversation`/`agentEvent`）；会话相关 L67 `conversationNew`、L268-277 `conversations*` 系列（list/summaries/get/search/save/pin/storyGraph/branch/delete/changed）。这是主进程 handler 与 preload 暴露面共用同一份常量、避免通道名漂移的契约源。
4. **`src/main/agent/runtime-session-binding-store.ts`（915 行）— agent 会话绑定状态机（agent 会话管理核心）**。`RuntimeSessionBindingStore` 接口 L64-94（`getByConversation`/`getById`/`claimAcpSession`/`rotateTransport`/`put`/`pruneClosed`/`listByController`）；三类冲突错误 L96-118（`RuntimeSessionClaimConflictError`「ACP session 已被另一 live binding 拥有」、`RuntimeSessionBindingConflictError`、`RuntimeSessionBindingCorruptionError`）；**不可变字段 + 单调游标 + 状态机监护 `assertBindingUpdateAllowed` L189-252**（immutable keys L193-199、modelBridgePolicy 不可变 L200-209、ACP session 只能走原子 claim 改 L210-214、单调游标不可回退 L215-221、active prompt 终态前不可变 L222-231、promptSequence 必须是下一个合法值 L232-247、状态转移白名单 L248-252）；`rotateBindingTransport` L255-...；SQLite 实现 `SqliteRuntimeSessionBindingStore` L320（CHECK 约束 L339-356）、`async claimAcpSession` L448-...（事务内 `assertSessionAvailable` 独占校验 L468）、内存实现 `MemoryRuntimeSessionBindingStore` L747。
5. **`build/agent-bundle.cjs`（1255 行）— 桌面分发的 agent runtime bundle 构建与验证核心**。命令入口 `build|import|verify|preflight` L1157-1158；锁定 runtime 输入校验 `lockedRuntimeInput` L137-169（targetName 派生 L138、缺锁定输入/digest 不匹配 fail-closed L148-166）；可信密钥注册 `readTrustedKeyRegistry` L100；路径穿越防护 `assertSafeManifestPath` L208、符号链接拒绝 L241、非支持文件拒绝 L249；**签名验证 `verifyManifestSignature` L484**（Node `verify` L512）、`verifyBundleDirectory` L524-534、`verifyLockedBundle` L1080-1085、矩阵验证 `verifyAgentBundleMatrix` L1096-1107；确定性 manifest（posix 相对路径 + mode + canonical bytes L257-293）；构建后验证钩子 L1012/L1069。

### 辅助读取（不计入 5 个核心文件）

- `src/main/window.ts`：`BrowserWindow` 安全配置 `webPreferences` — `preload: ../preload/index.cjs` L78、`contextIsolation: true` L79、`nodeIntegration: false` L80、`sandbox: true` L81。这是文件 2（preload 单向桥）成立的前提。
- `electron.vite.config.ts`（217 行）：主/渲染/preload 三入口打包；`deepSeekHarnessLlmVersion` 从 package.json 读取并注入内嵌 bundle manifest（L15-40）。
- `src/main/desktop-update-ipc.ts` / `desktop-update-service.ts` / `desktop-update-install.ts`：桌面自动更新链路（本步骤仅列举，深读阶段视方向取舍）。

### 负向选择（为什么不选）

- `src/main/ipc.ts`（7760 行）：体量最大的 IPC handler 实现，但通道契约已在 `ipc-channels.ts`、暴露面已在 `preload/index.ts`、挂载点已在 `main/index.ts:1593` 覆盖；深读阶段可作为「某个具体 handler」的下钻对象，不在本步骤 5 个核心文件内重复占用名额。
- `src/agent-daemon/*.ts`（几十个文件）：agent 守护进程的协议/ACP/model bridge 子模块，属 agent 会话的下游细节；会话绑定状态机已在 `runtime-session-binding-store.ts` 覆盖，避免同方向重复。
- `build/*.cjs`（其余）：签名/平台打包/性能门槛等脚本，分发主链已由 `agent-bundle.cjs` 覆盖。
- `docs/`、`tests/`：文档与测试，非实现文件。

## 三、工具/CLI 往返证据（acceptance: one tool/CLI link + daemon round-trip）

- **daemon RPC 往返（成功）**：`task.list`（workspace_id=1, instance=4baea3ff12c2ea5c）→ 取到本任务卡（title「深读 goodbuddy：Electron 桌面 agent 实现」）；`agent.register` → `{"status":"registered","agent_id":"callwarden-exec-worker-7f55","agent_instance_id":"callwarden-exec-worker-7f55-inst","session_id":"callwarden-exec-worker-7f55-sess","role":"executor"}`；`task.contract_get` → executor Role Contract prompt_hash `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6`；`task.claim` → 成功（status=in_progress，step S-1795466681671-2715d658，assignment_id=A-0d20275539010a7937f959d2，原始响应落盘 `runs/_claim_7f55.out`）。
- daemon 健康（Phase 0 预检）：`cw daemon health` → pid 2408，mTLS `https://127.0.0.1:4523`，schema 61；`cw daemon status 4baea3ff12c2ea5c` → active，registry_workspace_id=1193 / task_db_workspace_id=1 / instance 一致。

## 四、后续步骤实施计划（required_evidence: implementation plan）

1. 逐个深读 5 个核心文件，按「可直接落地 / 需改造 / 不适用」三分类提取借鉴点，每条带 `file:line` 证据；重点对齐 goodbuddy 三大方向：Electron 主/渲染分工（进程边界 + 权限门 + 安全 API 桥）、agent 会话管理（绑定状态机 + ACP session 原子认领 + 单调游标）、桌面分发（锁定 runtime + 签名校验 + 确定性 manifest）。
2. 回填 `docs/reports/testcode-11-repo-analysis-2026-11-23.md`：新增「goodbuddy 实现级深读」段（编号接续 63 起），更新第一节规模表、一·补深度分级表（goodbuddy 从「结构 + README 级」移入「实现级深读」）、第四节一句话定位（补深读要点）。
3. 负向验证：抽查每条引用在源码中逐字核对（防前序 REVIEW 的 file:line 错误类缺陷）；确认无「未读文件下结论」；自检三分类覆盖。
4. `workspace.register` 刷新 snapshot → `task.report`（success）→ 视响应 claim 后续步骤直至 review。

## 五、本轮环境记录

- goodbuddy 源目录为只读分析对象，不修改；交付物仅写入 worktree 内 git 跟踪的 `docs/evidence/` 与 `docs/reports/`。
- 行锚点全部来自本轮对源目录的实际 Read/Grep 输出，未依赖目录名或 README 猜测。
