# requirements-brief · ws-identity（workspace 身份仓库级归一）

> 2026-11-22 · R0 · 精简链（存量仓迭代：仓库存在、技术栈沿用 Python + Rust daemon）

## 一句话

把 `workspace_instance_id` 从「checkout 路径哈希」改为「仓库级身份」：同仓库所有 worktree / clone 共享同一身份，任务库、REQ 双向追溯、ac-map、验收证据、lease 全仓共享；含存量一次性迁移。

## 背景（证据）

- 现状身份按 checkout 路径派生：`server/daemon_client.py:188 derive_workspace_instance_id`（realpath 归一 + SHA-256 前 16 位）；Rust 侧同语义 `rust_ext/src/daemon/workspace.rs:409 normalize_identity_root`。
- 后果：同仓库 3 个 checkout = 3 个 workspace；任务 / REQ 追溯 / ac-map / 验收证据按 workspace_id 行过滤互不相通；lease 不跨 worktree。
- 停车场 PL-06 直接相关：registry 中 `c:/git_work/callwarden` 以 `c:/…` 与 `\\?\C:\…` 两种形式占 10 行（9 archived + 1 active），即路径书写形式未归一的卫生问题。

## 需求条目（REQ + MoSCoW）

| REQ | 优先级 | 内容 |
|---|---|---|
| REQ-01 | must | 仓库级身份派生：remote URL 归一化（协议形式 https/ssh、`.git` 后缀、大小写、尾斜杠、**剥离嵌入凭据**）优先 → 主仓 root 路径兜底 → 自身路径再兜底（fail-open 三级）。Python `derive_workspace_instance_id` 与 Rust `normalize_identity_root` 同语义改。 |
| REQ-02 | must | worktree / 主仓识别与注册链路对齐：`.git` 指针文件解析主仓（`server/agent_protocol.py:129-167` 已有解析）；`workspace.register` / reconciliation / principal 授权面按新身份工作。 |
| REQ-03 | must | 存量一次性迁移：同 repo 的多 workspace 行合并为一条 active，任务 / 证据 / lease 行 rewire（不合并任务，只改归属），旧 id 留映射表可审计；顺带清理 PL-06 的 `\\?\` 重复注册行。 |
| REQ-04 | must | 归一化矩阵单测：大小写、`\\?\` 前缀、正反斜杠、尾斜杠、remote 形式（https/ssh/带 `.git` 后缀/带凭据）、同目录不同书写 → 同一 identity；不同 repo / 不同 remote → 不同 identity。 |
| REQ-05 | should | 端到端进程级验收：主仓 + 2 个 worktree 三客户端注册得到同一 instance_id；A 在 worktree 建任务 B 在主仓可见；lease 跨 worktree 互斥。 |

## NFR

| 项 | 值 | 状态 |
|---|---|---|
| 兼容性 | R2 已关闭 O-01：register 返回值为权威，daemon 侧升级即身份收敛；旧客户端 derive 仅影响本地缓存键，无需双读兼容 | **closed**（R2 A-01 验证） |
| 性能 | 注册路径新增 git 解析（subprocess/IO）开销 | open（无来源数值，不发明 SLA） |
| 安全 | remote URL 嵌入凭据不得进入身份键与日志 | must 约束（并入 REQ-01） |

## 需求评审四件套

### 1. 方案对比表（身份主键）

| 维度 | 方案 A（推荐） | 方案 B | 方案 C |
|---|---|---|---|
| 做法 | remote URL 归一为主键；无 remote 回退主仓 root 路径；再无则回退自身路径（fail-open 三级） | 解析 worktree 的 `.git` 指针取主仓，主仓路径归一为主键；remote 仅记录 | remote URL 归一为主键；解析不出即拒绝注册 |
| 优点 | 同机多 clone 也合并；无 remote 场景可用；与现有 fail-open 风格一致 | 不依赖 remote；本地私有仓体验一致 | 键域最纯粹，无歧义 |
| 缺点 / 代价 | remote 变更（换 URL）会生成新身份，需迁移映射承接 | 同机两个独立 clone 不合并——只解决 worktree 不解决多 clone；remote 缺失时无兜底优势 | 裸目录 / 纯本地仓库 / 损坏 worktree 全部不可用 |
| 失效模式 | 多 remote / fork 场景可能把「同 URL 不同仓库」误合并（概率极低，URL 即仓库坐标） | 用户改主仓位置（移动目录）→ 身份漂移 | 单机用户完全被阻塞 |
| 适用条件 | git 托管为主 + 本地仓兜底 | 纯本地仓为主 | 强制 git 托管的产品定位 |
| 结论 | ✅ 选它（用户 R0 第 1 轮拍板） | ❌ 否决（见下） | ❌ 否决（见下） |

### 2. 被否决项及理由

- **否决 B**：同机多 clone 场景（`c:\repos\cw` 与 `c:\git_work\callwarden` 同 remote）仍按路径分裂，未达成「任务库全仓共享」的目标；且 worktree 指针解析 B 同样要做，并不更省。**重新考虑条件**：若 R2 发现 remote URL 归一化在多 remote / fork 场景产生错误合并。
- **否决 C**：非 git 目录、无 remote、悬空 worktree 直接不可用，违背单机自洽定位（callwarden 大量场景是本地仓 + 本地 daemon）。**重新考虑条件**：产品转为强制 git 托管。

### 3. 异常场景矩阵（8 类逐一过）

| # | 场景 | 结论 |
|---|---|---|
| 1 | 空值 / 缺省 | 无 remote / 非 git / 悬空 worktree → 三级兜底最终回退自身路径哈希 + warning（REQ-01）；迁移输入无 workspace 行 → 跳过并记录 |
| 2 | 无权限 | 归一不放宽权限：跨 workspace 访问仍按 workspace_id 行过滤 + principal 授权面；合并后 workspace 的 grants 沿袭规则见 O-02 |
| 3 | 重复请求 | register 幂等（同 identity 重复注册返回同一行）；迁移脚本按旧→新映射表去重，可重跑 |
| 4 | 冲突 | 迁移设计为「行 rewire 不合并任务」，冲突面收窄为 workspace 行合并（保留 active 行、其余归档）；并发首次注册同 repo → daemon 侧唯一约束 + upsert（R2 确认现有约束） |
| 5 | 依赖失败 | git 解析失败 → fail-open 回退；迁移中 daemon 不可用 → 迁移前备份 + 事务，失败整体回滚 |
| 6 | 重试 | 迁移可重跑（幂等）；git 解析为本地操作不重试 |
| 7 | 取消 / 恢复 | 迁移事务中断 → 回滚；映射表未落盘则整体重跑，无半成品 |
| 8 | 数据边界 | remote URL 嵌入凭据（`user:token@`）→ 剥离后再归一、不进日志；`\\?\` 扩展路径 → strip 前缀（PL-06）；非 ASCII 路径 → 沿用现有「仅 ASCII 小写」规则；hash 输出定长 16 hex 无超长问题 |

### 4. 未决项清单（三态）

| 类型 | 内容 | 责任人 | 验证方式 / 推翻条件 |
|---|---|---|---|
| confirmed | 主键 = remote 优先 + 路径兜底 | 用户 | R0 第 1 轮问答 |
| confirmed | 存量 = 一次性迁移 | 用户 | R0 第 1 轮问答 |
| confirmed | 异常 = fail-open 回退自身路径 | 用户 | R0 第 1 轮问答 |
| confirmed | 验收 = 端到端 + 矩阵单测 | 用户 | R0 第 1 轮问答 |
| assumption | A-01：身份权威计算在 daemon 侧 register（服务端归一），客户端 derive 仅本地缓存/展示 | Requirements Architect | **R2 已验证成立**（`daemon_client.py:3060-3063` 返回值权威 + 缓存），O-01 关闭 |
| assumption | A-02：同仓库多 clone（同 remote）合并为同一 workspace 是期望行为 | 用户 | R2 展示 register 实际行为确认；若不期望则退回方案 B |
| assumption | A-03：迁移合并 workspace 行时，principal grants 同 principal 多角色冲突按 owner > governance > agent > readonly 保最高权限，冲突记入迁移报告 | Requirements Architect | R3 卡片 C-04 落地；单用户本地部署下预计零冲突；若真实库出现高频冲突则改为 fail-closed 人工裁决 |
| confirmed | O-01 关闭：升级窗口旧客户端不会再生路径哈希新行（derive 仅本地缓存键，daemon 返回值为权威） | Requirements Architect | R2 `get_file_symbols` + 源码回读 |
| confirmed | O-02 关闭：grants 键域 = task-DB 数值 `workspace_id`（`workspace_principal_grants` PK (workspace_id, principal_id)，Rust `CAST(workspace_id AS TEXT)` 字符串精确匹配）；instance→数值 id 映射在 `workspace_authority_captures`（capture_revision 最新者胜）；snapshot 归属沿袭 = 迁移为旧 instance 追加指向存活 id 的新 capture 行（append-only，不改写历史） | Requirements Architect | R2/R3 源码锚定：`db_base.py:309-321`、`auth_principal.rs:587-596`、`dispatch.rs:3244-3289`、`dispatch.rs:343-368`、`workspace_reconciliation.rs:1-11` |
| confirmed | O-03 关闭：迁移工具形态 = `cw workspace migrate` CLI → daemon RPC `workspace.migrate`（daemon 单写点、事务、幂等、dry_run、对账报告） | Requirements Architect | R3 设计门禁；否决启动自动迁移（启动故障面不可控、无法对账确认）与脚本直连 SQLite（违反 C-09 daemon 单写点） |

## 降级声明

- semantic_search 三次查询均返回空（R0 前置检索降级）：**R2 已用 `get_file_symbols` + `get_symbol` + 源码回读完成正式锚定**（`symbol-anchor.md`），本降级不再延续。
- snapshot 正常（get_stats：429,446 符号 / generation 3），G4 无降级。

## 停车场动作

- PL-06（`\\?\` 重复注册行）：**纳入本轮** REQ-03 / AC-04。
- 新增 PL-08（本轮不做）：test-cw 验收执行的 worktree 隔离——验收在开发工作区跑会吸入在途改动。归属 test-cw 验收隔离，关闭条件：test-cw 强制在干净 worktree / 独立分支执行验收。