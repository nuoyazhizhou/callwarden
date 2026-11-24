# 证据 T-1795466681663-26a126dc · 步骤 S-1795466681671-27164480：goodbuddy 深读可借鉴点提取

- 任务：T-1795466681663-26a126dc「深读 goodbuddy：Electron 桌面 agent 实现」
- 步骤：S-1795466681671-27164480（step_index 1，action=逐个深读并提取可借鉴点，assignment A-770fbdf680d1fbd6b7fdb318）
- 认领身份：agent_id=callwarden-exec-worker-7f55，role=executor，session=callwarden-exec-worker-7f55-sess
- 分析对象：`c:/git_work/callwarden/testcode/goodbuddy`，HEAD `d6e44a68d9bccd74f39e624601bac89ea7809bd7`
- 深读文件：步骤1 选定的 5 个核心实现 + 辅读 `src/main/window.ts` / `src/main/trusted-ipc-sender.ts` / `src/main/ipc.ts`
- 编号：接续报告「二·补 D」的 62，本轮 63-73（可直接落地 63-68 / 需改造 69-71 / 不适用 72-73）
- 取证纪律：每条带 `file:line`，全部来自本轮对源目录的实际 Read/Grep 输出；三分类判定口径同报告既有段。

## 可直接落地（63-68）

63. **contextBridge 单向白名单桥 + sender 三重校验**（`src/preload/index.ts:1,225-... ,2114` + `src/main/trusted-ipc-sender.ts:6-17`）：preload 侧 `desktopApi` 只暴露 `ipcRenderer.invoke(on)` 白名单方法（L225 起 `openRuntimeNativeClient`/`storageUpgrade`/`app` 等逐一定义），唯一出口 `contextBridge.exposeInMainWorld('goodbuddy', desktopApi)` L2114——渲染进程拿不到 ipcRenderer 本体；主进程侧 `assertTrustedSender` 对每个 invoke handler 做三重校验：`event.sender === window.webContents && event.senderFrame === window.webContents.mainFrame && event.senderFrame.url === window.webContents.getURL()`（`trusted-ipc-sender.ts:8-14`），不满足抛「拒绝来自未知窗口的 IPC 请求」（L15）；`ipc.ts` 中该断言覆盖 30+ handler（L3263/3286/3295/3306/...）。配套 `window.ts:78-81` 的 `contextIsolation:true / nodeIntegration:false / sandbox:true`。**对 callwarden**：daemon 的 mTLS HTTPS 是传输层信任，但工具/RPC handler 层可加同形态「调用方身份 == 绑定会话身份」的断言（callwarden 已有 lease/identity fencing，此为 doors 层的对称加固）；agent 侧工具面只暴露白名单方法、不给底层 invoke 句柄，与工具面 profile 治理同向。

64. **共享通道常量契约**（`src/shared/ipc-channels.ts:1,67,69-74,268-277`）：`export const ipcChannels` L1 单一对象集中所有通道名；主进程 handler（`ipc.ts`）与 preload 暴露面（`preload/index.ts`）import 同一份常量（主进程 import 见 `index.ts:25`），通道名不会两边漂移。agent 通道 L69-74（status/run/cancel/questionRespond/compact/event），会话通道 L268-277（list/summaries/get/search/save/pin/storyGraph/branch/delete/changed）。**对 callwarden**：MCP 工具名 / RPC method 应有一份生成式共享契约（项目已有 `scripts/gen_route_matrix.py` 记录 MCP 名≠RPC method 的映射，正是缺这份单一契约的痛点）；把通道名收进单一常量源，可消除「客户端 derive 名称」类漂移。

65. **主进程权限门白名单 + 来源绑定**（`src/main/index.ts:596-617`）：`app.whenReady()` L596 内 `session.defaultSession.setPermissionRequestHandler` L599-611 只放行「permission===media && webContents===mainWindow.webContents && mediaTypes 含 audio 且不含 video」；`setPermissionCheckHandler` L613-617 同口径。默认拒绝一切未显式放行的权限，且绑定到具体 webContents 身份。**对 callwarden**：「默认拒绝 + 显式白名单 + 绑定到具体主体身份」是 lease/authorization 的通用形态；可用于 daemon 出站能力（网络/文件/shell）的默认拒绝门禁。

66. **secureCipher 的平台感知可用性判定（fail-closed）**（`src/main/index.ts:673-684`）：`isAvailable()` 要求 `safeStorage.isEncryptionAvailable()` 且 Linux 上必须是 `gnome_libsecret/kwallet/kwallet5/kwallet6` 之一（L675-682），否则视作不可用（RuntimeSettingsStore 拿到不可用 cipher 走非加密降级路径）；加解密 L683-684。**对 callwarden**：secret/token 落盘前应做同形态「平台后端可用性 + 已知安全后端白名单」判定，后端不满足时 fail-closed 而非静默用弱加密。

67. **会话绑定状态机 + 不可变字段/单调游标监护**（`src/main/agent/runtime-session-binding-store.ts:189-252`）：`assertBindingUpdateAllowed` 对每次更新强制六类不变量——immutable keys 不可变（L193-199）、modelBridgePolicy 不可变（L200-209）、ACP session 只能走原子 claim 修改（L210-214）、单调游标 BigInt 不可回退（L215-221）、active prompt 操作 id 终态前不可变（L222-231）、promptSequence 必须是下一合法值（L232-247）、状态转移白名单（L248-252）；七态集合见 L339-348。冲突分类为三类专属错误 L96-118。**对 callwarden**：任务 lifecycle 状态机（open→in_progress→review→applied→closed）可执行同形态监护函数——不可变字段（task_id/contract hash）、单调 revision、prompt/lease 身份终态前不可变、状态转移白名单，把「非法跃迁」从约定变成代码级门禁。

68. **SQLite 部分唯一索引表达「活绑定唯一」+ STRICT + WAL/FULL**（`src/main/agent/runtime-session-binding-store.ts:313,328-369`）：`liveBindingPredicate = "state <> 'closed'"` L313；两张 **部分唯一索引** 保证同一 conversation / 同一 acp_session 至多一条活绑定（`WHERE state <> 'closed'`，L359-366），closed 行留作历史不冲突；表 `STRICT` + 各字段 `CHECK(length BETWEEN...)` + state `CHECK IN(...)` L335-354；`PRAGMA journal_mode=WAL / synchronous=FULL / foreign_keys=ON` L331-333。**对 callwarden**：callwarden 单 SQLite-per-host 的 lease/snapshot 表可直接借「部分唯一索引 + 谓词」表达「同一资源至多一条活租约」——无需应用层轮询判重，DB 层原子保证；`state<>'closed'` 谓词与「保留历史 + 唯一活跃」语义天然契合。

## 需改造（69-71）

69. **会话 ACP 原子认领 + 容量硬上限 + 条目驱逐**（`src/main/agent/runtime-session-binding-store.ts:448-...,654-690,313`）：`claimAcpSession` L448 在事务内做 `assertSessionAvailable(acpSessionId, bindingId)` L468 独占校验（全局唯一活 ACP session，冲突抛 L96 专属错误），状态 closed 不可认领 L461-465；`maximumRows` 硬上限（L40/L330），写入超限时按策略驱逐最旧条目（`required = count - maximumRows + 1` L654、`remaining.count >= maximumRows` L664、`enforceExistingCapacity` L671-690），`MemoryRuntimeSessionBindingStore` 同语义 L892。**改造点/依赖**：callwarden 的 lease 已有 fencing，但「同一资源原子认领 + 全局唯一活绑定 + 有界容量 + 超限驱逐」形态可用于 daemon 会话/任务绑定表；callwarden 需保留历史审计，驱逐策略应改为「归档而非删除」，不直接照搬 LRU 删除。

70. **utilityProcess fork 的最小权限启动参数**（`src/main/index.ts:441-... ,481-... ,494-...`）：Continue host fork 用 `utilityProcess.fork(..., {cwd, env, serviceName, stdio:'pipe'})` L441-451；DeepSeek harness fork 显式 `allowLoadingUnsignedLibraries:false`(L483), `disclaim:false`(L484)，fork 块 L481-486（默认拒绝加载未签名库）；WeChat sidecar 用 `stdio:'ignore'` L494 起。**改造点/依赖**：callwarden daemon 派生的 helper/子进程可对齐「默认禁未签名库 + 显式 serviceName 便于审计 + 最小 stdio」；callwarden 是 Rust daemon，spawn 形态不同，借的是参数纪律而非 Electron API。

71. **签名 manifest 的确定性规范化 + 可信密钥注册**（`build/agent-bundle.cjs:274-293,484-521,100,241-249`）：`canonicalManifestBytes` L274 + `createManifest` L278-293 把目录快照规范成 posix 相对路径 + mode + canonical JSON bytes；`verifyManifestSignature` L484-521 依次做 JSON 解析 → `validateManifestShape` → **规范化字节比对（manifest 必须确定性canonical，否则拒）L497** → 从可信注册 `trustedKeyForManifest`（registry L100）取 `publicKeySpkiBase64` → 检查签名长度 64 + `verify(null, signaturePayload(manifestBytes), publicKey, signatureBytes)` L511-516；`assertSafeManifestPath` L208 防路径穿越、L241 拒符号链接、L249 拒非支持文件；命令 `build|import|verify|preflight` L1157-1158。**改造点/依赖**：callwarden 已有 snapshot/evidence hash，可借用「确定性 canonical 字节 + 签名 + 可信密钥注册 + 路径穿越/符号链接防护」的发布验证链；callwarden 的可信根应绑定 workspace instance 与 principal，不照搬桌面端 registry。

## 不适用（72-73）

- **Electron contextIsolation / sandbox 三件套本身**（`window.ts:78-81`）：是桌面渲染进程的安全模型，callwarden 是 mTLS RPC 服务 + CLI/MCP 工具面，无 BrowserWindow 渲染进程。其派生的「白名单桥 + sender 校验」以类比形态见 63。
- **agent bundle 的 Node runtime 锁定与平台矩阵打包**（`build/agent-bundle.cjs:137-169,1080-1107,1157-1158`）：`lockedRuntimeInput` 按 targetName 校验锁定 Node 版本/digest、`verifyAgentBundleMatrix` 跑多架构矩阵，是 Electron 桌面分发特有问题；callwarden 走 wheel/Rust 二进制分发，无 Node runtime 内嵌需求。其派生的「digest fail-closed + 签名验证」以类比形态见 71。

## 负向验证（negative test）

- 逐字核对：本轮 10+ 个关键锚点经脚本对源目录实际行内容比对，全部命中（含 `trusted-ipc-sender.ts:6` assertTrustedSender、`runtime-session-binding-store.ts:313` liveBindingPredicate、`agent-bundle.cjs:497` canonical 比对、`index.ts:599` setPermissionRequestHandler）。无「未读文件下结论」。
- 三分类自检：每条均标注对 callwarden 的意义（不适用项也说明派生形态归属），无空泛结论。
- 相对前序：本轮未重跑 build_graph（goodbuddy 走图谱路径但报告已到结构级；本轮为源码级深读），所有结论为直接源码取证。
