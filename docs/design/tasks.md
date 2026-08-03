# Implementation Plan: 多 LLM 契约驱动协同

## Overview

本计划以 Python 3.9+ 和现有 SQLite/Mixin/CLI/MCP 架构实现 `multi-llm-contract-collaboration`，daemon 侧补齐工作在现有 PyO3 Rust 扩展 `rust_ext/` 内完成。实施顺序严格为 `P0 → D0 → P1 → P2 → P3 → P4`：P0 先在**不修改 schema**的前提下验证 blind-first review；D0 补齐跨平台 daemon 基座；只有 G0 与 GD 同时通过，才允许开始 P1。后续阶段也必须依次通过迁移、自动化测试与能力声明门禁，禁止提前并行产品化。

**D0 是 P1 的前置阶段**：Requirements 6.19、11.2、11.4、11.9、11.10 都直接引用 daemon 串行化点与 Authoritative_Clock，因此 P1 的 Evidence 追加、gate decision 提交与 P4 的 lease 时间语义在 D0 交付前没有落地载体。

**P0 不依赖 D0**：Requirement 13.17 规定 P0 独立于 P1–P4，也不依赖 daemon 基座；因此 D0 可与 P0 并行推进，D0 的进度不阻塞 P0 批次，G0 与 GD 相互独立。

**"相互独立"的适用层面**：上述独立性只在**阶段门禁层面**成立——D0 不必等 G0，P0 也不必等 GD。在**执行层面**，D0 中触碰 `cli/main.py` 与 i18n 文件的子任务（3.13、3.14）与 P0 的 CLI 接入子任务（1.4）共享同一批文件，由依赖图的 wave 顺序串行化（1.4 在 wave 3，3.13 在 wave 11，3.14 在 wave 12）。不得把"D0 与 P0 相互独立"理解为可以真并行编辑这些共享文件。

**D0 的两项已决策**：

1. **Windows 传输定为命名管道**（Requirements 14.2、14.18–14.21）。判据只有一条：操作系统是否为该连接提供不可伪造的对端身份。命名管道名由 owner user SID 派生（`\\.\pipe\callwarden-<user-sid>`），安全描述符仅授权 owner SID（可选 local administrators），访问范围等价 Unix domain socket 的 owner + 0660；daemon 维持 ≥2 个管道实例并在服务每个已接受连接之前补建替换实例，消除 accept 之间的竞态窗口。Windows AF_UNIX、localhost TCP 与本机 HTTPS 端点全部排除，因为 OS 不为其提供 Peer_Credential，Requirement 14.5 无法成立。
2. **daemon 不可用时"先唤起 + 按操作分级"**（Requirements 14.22–14.33）。客户端连不上端点时先尝试启动 daemon，并在有界等待窗口（默认 10 秒、按客户端时钟计量）内指数退避重试，成功即在该连接继续原请求；启动前必须取跨进程互斥，保证同一用户端点最多一个 daemon 进程，不产生第二个串行化点。唤起失败后进入 Degraded_Mode 并按 `class(op)` 分流：只读查询与 Index_Write 允许直连 SQLite，Governance_Write 一律 fail closed 并返回带稳定错误码、i18n key 与可执行恢复指引的 Structured_Reason；降级模式下的所有操作都记录 Degraded_Mode 标记与降级原因。降级产物因缺少 daemon 签发的有效 Attestation 而判 invalid，因此不需要任何物理写屏障。

**P4 Lease 的正面边界**（Requirements 11.13、14.32）：Lease 是 daemon 在线期间的并发正确性保证；防篡改归属于 Attestation 校验与追加式 Evidence_Ledger，任何代码、CLI 输出与文档都不得把 Lease 描述为能防止离线直接改库。

### 阶段门禁

- **G0（P0→P1）**：至少 30 个有效任务且至少 10 个非平凡 `code_change`（按 Requirement 12.26 的行数与符号门槛判定）；Requirements 12.10–12.13 全部满足；Requirements 12.15–12.20 无暂停条件；Requirements 12.27–12.29 的灰区观察不得未决；至少 90% Treatment 可证明 verdict-before-reveal 且 blind view 无禁止字段。未满足时只报告方向、恢复现有 review 流程并创建新批次，不得执行任何 P1 schema/API 任务。
- **GD（D0→P1）**：Windows 命名管道传输与服务化、命名管道多实例保活与 accept 竞态消除（≥2 实例且服务前补建）、管道名按 owner SID 派生且 SDDL 仅授权 owner SID、端点负向约束（无 Windows AF_UNIX、无监听 TCP 端口、无本机 HTTPS 协同 RPC 入口）、Windows 对端令牌 SID 等强度 ACL（覆盖 Unix build 每一个 UID 校验点）、macOS 无 pid 身份派生与 launchd 端到端验收、三平台自动唤起可用且并发唤起只产生一个 daemon（一个串行化点）、Degraded_Mode 三类分流正确（只读与 Index_Write 直连成功、Governance_Write fail closed 且 Structured_Reason 含平台具体恢复指引）、Degraded_Mode 标记与降级原因可审计区分、缺少有效 Attestation 的记录判 invalid 且不满足任何 Blocking_Clause、daemon 进程内唯一串行化点、Authoritative_Clock、daemon 侧 Attestation 签发、并发读写无数据库锁错误与并发 gate 快照隔离、Stage_Toggle 三级作用域与前置阶段校验、daemon 配置存储同时承载 Stage_Toggle 与 Independence_Policy 的取值与变更审计（每次变更带发起者 Peer_Identity 与 Authoritative_Clock 时间、禁止由单次请求参数设置；政策语义本身在 P1 落地，随 4.5 验收）、跨类操作按组成部分分级正确（`components(op)` 拆分、Index_Write 组成部分直连执行、Governance_Write 组成部分 fail closed、任务与步骤状态不推进、不产生 Evidence 与 gate decision；`task_report_step` 实例的端到端验收随 4.8/4.28 在 P1 落地）、Stage_Toggle 存储过渡与保值迁移通过验收（`Experiment_Batch_Config` 承载期无 schema 变更、迁移按原作用域保值且带发起者与权威时钟时间、迁移后只读 daemon 存储）、稳定错误码目录与 `zh_CN`/`en_US` 双语 i18n 解析，全部通过自动化验收。任一项未通过时，不得开始任务 4（P1）的 schema/API 任务。GD 与 G0 相互独立：P0 可先于 D0 完成，但 P1 必须同时满足 G0 与 GD。
- **G1（P1→P2）**：P1 migration、Envelope/View/Verdict/Evidence/Gate、CLI/MCP、属性与集成测试全部通过；`task_report_step` 与 `task_apply` 已共享门禁语义，`task_close` 仅收尾。
- **G2（P2→P3）**：artifact/interface 依赖、provider 解析与环检测迁移及测试通过；未引入复杂 DAG 调度。
- **G3（P3→P4）**：agent/session/model identity、attestation、blind 顺序与独立审核证明迁移及测试通过；身份不等同 ownership。
- **G4（完成）**：assignment 与安全 lease 的 token/expiry/renew/release/fencing、protected mutation 和 Evidence Gate 组合测试通过。

### 并行与所有权规则

- 每个子任务的“所有权”列出该任务唯一可修改的生产文件、测试文件或文档；同一 wave 内不得扩大到其他任务拥有的文件。
- 若实现中发现必须修改未列出的共享文件，先停止该 wave、调整依赖图并重新分配所有权，不得让两个并行任务同时编辑同一文件。
- `db/db_tasks.py`、`db/db.py`、`db/schema.py`、`db/db_base.py`、`cli/main.py`、`server/mcp_server.py` 与 i18n 文件均按 wave 串行修改。
- D0 涉及的既有 daemon 文件同样按 wave 串行修改，不得并行编辑：`rust_ext/Cargo.toml`、`rust_ext/src/daemon/server.rs`、`rust_ext/src/daemon/workspace.rs`、`rust_ext/src/daemon/peercred.rs`、`rust_ext/src/daemon/protocol.rs`、`rust_ext/src/daemon/attestation.rs`、`rust_ext/src/daemon/config.rs`、`rust_ext/src/daemon/dispatch.rs`、`rust_ext/src/daemon/mod.rs`、`server/daemon_client.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
- **D0 与 P0 之间的共享文件由 wave 顺序串行化**：D0 在**阶段门禁层面**不依赖 G0（可先于 P0 完成，也可与 P0 交错推进），但在**执行层面**，D0 中触碰 `cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json` 的子任务（3.13、3.14，以及后续新增的同类子任务）与 P0 的 CLI 接入子任务 1.4 共享同一批文件。这些文件的编辑顺序由依赖图的 wave 全局顺序保证：1.4（wave 3）必须先完成，3.13（wave 11）与 3.14（wave 12）才能开始。禁止把"D0 与 P0 相互独立"理解为可以真并行编辑这些共享文件；若新增子任务需要触碰同一批文件，必须落在这些 wave 之后的独立 wave。
- Rust daemon 侧 ACL 校验点或方法清单（含 admin-only/readonly 清单）变更后，必须运行完整 daemon 测试集 `cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib` 并逐项处理失败；不得以新增 ACL 用例或局部模块测试通过替代完整回归。
- Windows 与 WSL 不得共用 Cargo target 目录：Linux 侧验收必须设置独立目标目录（例如 `CARGO_TARGET_DIR=/tmp/callwarden-target cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`），避免跨文件系统锁等待与遗留编译进程。
- 标记 `*` 的测试子任务可为 MVP 跳过；所有未标记 `*` 的实现、迁移、接线和文档同步任务不可跳过。

## Tasks

- [ ] 1. P0：在不改 schema 的前提下完成 blind-first review 对照实验工具链
  - [x] 1.1 实现实验批次与冻结协议模型
    - 新增批次配置、纳入/排除规则、分层维度、随机种子、指标分子/分母、观察窗口、成功/暂停阈值和 invalid 原因；首次纳样后禁止原地改协议，规则变化必须产生新批次。
    - 仅使用文件/JSONL 记录并明确标记 `non_product_evidence`；不得修改 `db/schema.py`、`db/db_base.py` 或创建产品 Evidence 表。
    - `Experiment_Batch_Config` 在 daemon 配置存储不可用期间承载 P0 的 Stage_Toggle：支持 global/workspace/task 三级作用域，每次变更记录发起者 session marker 与**客户端时钟**时间（该窗口内 daemon 尚未交付，Authoritative_Clock 不存在），且不引入任何 schema 变更；P0 开关解析不读取 P1–P4 的任何开关取值。
    - **所有权**：新增 `experiments/__init__.py`、`experiments/blind_review_protocol.py`。
    - **依赖门禁**：无；这是第一项实施任务。
    - _Requirements: 12.1–12.3, 12.8–12.9, 12.21, 12.23–12.24, 13.1, 13.6–13.8, 13.18, 13.21_

  - [x] 1.2 实现 Control/Treatment 最小披露投影与追加式 JSONL 采集
    - Control 在首轮 verdict 前包含契约事实、代码事实和 Implementer Notes；Treatment 使用 allowlist，仅包含契约、actual changes、符号变化、测试/静态事实，并在首轮 verdict 后记录 reveal 与结构化变更原因。
    - Minimal_Blind_View 只由现有字段构成（任务标题/描述、`task_steps` 的 target_file/target_symbol、`change_audit` diff、`task_symbol_changes`、既有 `test_runs` 状态、open `task_quality_findings`）；同时记录披露字段清单与排除字段清单，并标注为实验披露清单而非 View_Manifest。
    - 复用现有任务状态、`change_audit`、`task_symbol_changes`、quality findings、跨会话 apply/reopen 的只读导出；禁止存储隐藏推理历史。
    - **所有权**：新增 `experiments/blind_review_views.py`、`experiments/blind_review_jsonl.py`。
    - **依赖门禁**：完成 1.1。
    - _Requirements: 12.1, 12.4–12.8, 12.18, 12.20, 12.23, 12.25, 13.2–13.6_

  - [x] 1.3 实现实验评估、成功判定、灰区标记与 fail-safe 暂停状态机
    - 计算绝对计数、比例、置信区间、recall/false-positive/latency/reopen/rollback/blinding 指标；有效样本不足时只能输出 directional result。
    - 非平凡 `code_change` 最小样本判定要求至少一个 tracked 源文件改动 10 行以上非注释代码且 `task_symbol_changes` 至少一条符号变化，排除纯格式化与生成文件改动。
    - false-positive 高出 Control 10–20 个百分点、median latency 增幅 25%–50% 判为灰区：标记批次未授权 P1、继续纳样并记录为灰区观察；灰区未解决前不授权 P1，但灰区本身不触发暂停，暂停触发器仍只有 Requirements 12.15–12.20。
    - 任一暂停条件触发时停止新样本、保留批次与阈值、恢复现有 review 流程；暂停动作无法记录时默认拒绝新纳样。
    - **所有权**：新增 `experiments/blind_review_evaluator.py`。
    - **依赖门禁**：完成 1.1、1.2。
    - _Requirements: 12.6, 12.8–12.24, 12.26–12.29_

  - [x] 1.4 接入 P0 CLI，提供批次冻结、纳样、verdict/reveal 记录、暂停与报告命令
    - 在 `cw` 下提供明确的 experiment 命令，输出机器可读 G0 决策及失败原因（含灰区观察状态）；不得把 P0 记录称为产品 Evidence 或开放 P1 hard gate。
    - 为中英文输出补齐消息键，并保持既有 task apply/reopen 接口行为不变。
    - **所有权**：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 1.2、1.3。
    - _Requirements: 12.1–12.29, 13.1, 13.3, 13.9–13.10_

  - [ ]* 1.5 编写 P0 协议、分组和指标单元测试
    - 覆盖分层随机可复现、协议锁定、invalid 样本排除、最小样本与非平凡门槛、全部成功阈值、每个灰区边界与每个暂停阈值边界。
    - **所有权**：新增 `tests/test_blind_review_experiment_unit.py`。
    - **依赖门禁**：完成 1.1–1.3。
    - _Requirements: 12.2–12.29_
  - [ ]* 1.6 编写 P0 与现有任务状态机的集成测试
    - 用临时数据库和文件记录验证 `task_report_step → review → cross-session task_apply/task_reopen` 两组流程；断言 P0 不新增表、不改变状态转换且泄露、同会话、snapshot 漂移样本失效。
    - 断言 Minimal_Blind_View 只使用现有字段，实验期间 schema 版本不变（对应 Property 24）。
    - **所有权**：新增 `tests/test_blind_review_experiment_integration.py`。
    - **依赖门禁**：完成 1.2–1.4。
    - _Requirements: 12.1, 12.4–12.8, 12.15–12.20, 12.25–12.26, 13.2–13.5_

  - [ ]* 1.7 编写 P0 报告完整性与 JSONL 恢复测试
    - 验证中断恢复、追加记录不丢失、每个 invalid 原因可见、指标分子/分母和观察窗口随报告输出，且不能通过修改旧批次移动目标线。
    - **所有权**：新增 `tests/test_blind_review_experiment_reporting.py`。
    - **依赖门禁**：完成 1.2、1.3。
    - _Requirements: 12.21–12.24, 12.27–12.29_

  - [~] 1.8 同步 P0 CLI 与阶段能力文档
    - 记录 P0 命令、实验记录位置、G0 判定、灰区语义、暂停恢复和“非产品 Evidence”限制；D0 与 P1–P4 均标记为 planned/unavailable。
    - **所有权**：`docs/cli_reference.md`、`docs/design/implementation-status.md`、`docs/agent-usage-guide.md`。
    - **依赖门禁**：完成 1.4。
    - _Requirements: 12.14, 12.21–12.24, 13.1, 13.6–13.10_

- [~] 2. G0 检查点：确认 P0 自动化测试通过，并仅在真实批次输出 `eligible_for_p1=true` 时继续
  - Ensure all tests pass, ask the user if questions arise.
  - 若样本不足、任一成功条件未满足、存在未解决灰区或任一暂停条件触发，停止在此处；保留记录并通过新批次复验，禁止开始任务 4（P1）。
  - 任务 3（D0）与 P0 相互独立（Requirement 13.17），不受 G0 阻塞，可与 P0 并行推进。

- [ ] 3. D0：跨平台 daemon 化（P1 的前置阶段，与 P0 相互独立）
  - **已决策事项**：Windows 传输为命名管道（14.2、14.18–14.21）；daemon 不可用时先自动唤起、唤起失败后按操作类别分级（14.22–14.33）。本阶段实现自动唤起与 Degraded_Mode 分级，不实现任何物理写屏障——降级产物由 Attestation 校验判 invalid 兜底。
  - **P4 Lease 的正面边界表述要求**：本阶段产出的代码注释、CLI 输出与文档一律按 Requirements 14.32、11.13 正面陈述——Lease 是 daemon 在线期间的并发正确性保证，防篡改归属 Attestation 与追加式 Evidence_Ledger；禁止出现"Lease 不可绕过"或"Lease 能防止离线改库"这类表述。
  - **GD 检查点**：3.1–3.34 全部完成后，确认 GD 的每一项都通过自动化验收再进入任务 4。Ensure all tests pass, ask the user if questions arise.
  - [~] 3.1 抽象平台无关的 daemon 监听/接受/请求循环并实现 Windows 命名管道端点
    - 把当前只在 Unix 编译的监听、接受连接与请求循环抽象为平台无关的 listener/acceptor/connection 抽象，Unix 侧绑定 Unix domain socket，Windows 侧绑定命名管道 Daemon_Endpoint。
    - 三平台暴露等价的协同 RPC 方法集（Envelope 发布、verdict 提交、Reveal、Evidence 追加、gate 评估、Protected_Mutation 路由）；缺方法即视为该平台不支持协同能力，不实现“只支持只读”的中间态。
    - **管道命名与 SDDL**：管道名由 owner user SID 派生（`\\.\pipe\callwarden-<user-sid>`），安全描述符只授权 owner SID 的 connect 与读写（可选附加 local administrators），其他 SID 一律不授权，使访问范围等价 Unix socket 的 owner + 0660。
    - **实例保活（硬要求）**：预创建 ≥2 个管道实例，并在服务每个已接受连接**之前**补建替换实例，消除两次 accept 之间的 pipe-busy / 端点缺失竞态窗口。这不是优化项，顺序颠倒即重新引入竞态。
    - **端点负向约束**：实现与配置层面不得存在 Windows AF_UNIX 端点、监听 TCP 端口或本机 HTTPS 协同 RPC 入口；OS 不为这些连接提供 Peer_Credential，Requirement 14.5 在其上无法成立。
    - **所有权**：新增 `rust_ext/src/daemon/transport.rs`、`rust_ext/src/daemon/transport_windows.rs`。
    - **依赖门禁**：完成 3.24（Windows 平台依赖已就位）；不依赖 G0。
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.18, 14.19, 14.20, 14.21_

  - [~] 3.2 改造 daemon server 去掉 Unix-only 编译门并接入传输抽象
    - 移除 `server.rs` 顶部的 `#![cfg(unix)]`，把监听、接受与请求循环改为调用 3.1 的传输抽象；Windows 上 daemon 可启动、可托管、可自恢复。
    - 在 `mod.rs` 中按平台导出传输模块，保持 Unix 行为不回归。
    - **所有权**：`rust_ext/src/daemon/server.rs`、`rust_ext/src/daemon/mod.rs`。
    - **依赖门禁**：完成 3.1。
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

  - [~] 3.3 实现三平台 Peer_Credential → Peer_Identity 派生
    - Windows 从命名管道对端访问令牌取 SID 作为 Peer_Identity；Linux 用 `SO_PEERCRED` 的 UID/GID（pid 仅审计）；macOS 用 `LOCAL_PEERCRED` 的 UID/GID 并显式排除 pid，缺 pid 不得退化为无身份或拒绝全部请求。
    - 客户端自报的 agent 名、session 名与请求体身份字段一律不参与授权判定，只作审计元数据。
    - **所有权**：`rust_ext/src/daemon/peercred.rs`。
    - **依赖门禁**：完成 3.1。
    - _Requirements: 14.5, 14.8, 14.9_

  - [~] 3.4 实现 Windows SID 等强度路径 ACL，替换非 Unix 分支的跳过行为
    - 枚举 Unix build 中所有由 UID ACL 保护的路径校验点（重点是 `_validate_owned_path` 当前 `#[cfg(not(unix))]` 分支直接跳过 UID 检查），逐点补上对端令牌 SID 与注册 workspace owner SID 的等价比较。
    - SID 不匹配时返回 Structured_Reason 并拒绝路径访问，不得回退到跳过 ACL 或按“尽力校验”处理。
    - 在本文件的 `#[cfg(test)]` 模块内实现 3.5 所需的**单元层 mock 用例**：伪造非当前 UID/GID（Unix）或非 owner SID（Windows）的 Peer_Credential，断言 owner 比较不匹配时走拒绝路径、返回 Structured_Reason 且状态不变。该层只覆盖判定逻辑，不替代 3.5 的真实跨用户连接验收。
    - **所有权**：`rust_ext/src/daemon/workspace.rs`。
    - **依赖门禁**：完成 3.3。
    - _Requirements: 14.5, 14.9_

  - [~] 3.5 补齐 macOS launchd 打包与三平台端到端验收
    - 增加 macOS launchd 打包/启动配置与验收脚本；验收覆盖 daemon 启动、无 pid 身份派生、ACL 拒绝路径与协同 RPC 方法集等价性。
    - Windows 验收覆盖命名管道启动、服务化托管与 SID ACL 拒绝路径；任一平台未通过即视为该平台不支持本文协同能力。
    - 验收同时覆盖三平台自动唤起：Windows 分离进程在客户端退出后仍存活、macOS 经 launchd 激活已注册 user agent、Linux 经 systemd 用户级服务激活；并断言停掉 daemon 后并发发起多会话请求只产生一个 daemon 进程、一个串行化点，且所有请求在同一端点完成。
    - **macOS 跨用户 ACL 的两层验收实现方式**（GHA macOS runner 默认单用户，无法天然构造跨用户连接，因此必须显式指定实现方式）：
      - **单元层**：用 mock 伪造非当前 UID/GID 的 Peer_Credential，断言 owner 比较不匹配时走拒绝路径并返回 Structured_Reason。该用例由 3.4 在 `rust_ext/src/daemon/workspace.rs` 的 `#[cfg(test)]` 模块内实现（该文件由 3.4 所有），本任务只验收其存在并通过。
      - **集成层**：在本任务所有权内的 e2e 脚本中用 `sudo -u` 创建临时测试用户，以该用户身份发起 Unix domain socket 连接，真实验证 macOS ACL 阻断。
      - **两层都必需的理由**：mock 覆盖判定逻辑（比较分支、错误码、状态不变），真实连接覆盖 `LOCAL_PEERCRED` 的实际行为与 socket 文件权限（0660 + owner）是否真的挡住其他用户。mock 绕过了内核凭证获取与文件系统权限检查这两个真正的执行点，因此不能替代真实连接；反之真实连接也不便穷举拒绝分支，两层互补而非二选一。
    - **所有权**：`.github/workflows/e2e/run_platform_e2e.py`、`.github/workflows/e2e-verify-macos-arm64.yml`、`.github/workflows/e2e-verify-windows-amd64.yml`、`.github/workflows/e2e-verify-linux-x86_64.yml`。
    - **依赖门禁**：完成 3.2、3.3、3.4、3.6、3.25、3.26。
    - _Requirements: 14.1, 14.3, 14.8, 14.22, 14.23, 14.24, 14.25, 14.26_

  - [~] 3.6 实现 Windows `canonical_bytes_b64` 载荷路径校验
    - SCM_RIGHTS FD 传输仅 Unix 可用，Windows 客户端通过既有 `canonical_bytes_b64` 参数直接提交规范化字节；使用前必须校验载荷尺寸不超过配置上限且实际内容摘要等于请求声明的摘要。
    - 任一校验失败返回 Structured_Reason 并拒绝请求，不得按“尽力解析”继续。
    - **所有权**：`rust_ext/src/daemon/protocol.rs`。
    - **依赖门禁**：完成 3.1。
    - _Requirements: 14.10_

  - [~] 3.7 实现 daemon 进程内唯一串行化点、请求队列与请求超时
    - Protected_Mutation 全部经由 daemon 进程内单一串行化点应用，写请求在该点排队；系统不得暴露第二个串行化点。
    - 格式正确的并发读写请求在配置的请求超时内完成，超时返回结构化超时原因且不改变任务状态；SQLite 写锁在 P0–P4 全阶段降为纯事务互斥，不参与授权、ownership、lease 与 Independent_Review 判定。
    - **所有权**：新增 `rust_ext/src/daemon/serialization.rs`；`rust_ext/src/daemon/dispatch.rs`（仅 Protected_Mutation 路由与队列接线）。
    - **依赖门禁**：完成 3.2、3.8。
    - _Requirements: 14.6, 14.7, 14.14_

  - [~] 3.8 实现 Authoritative_Clock API
    - 以 daemon 进程时钟作为唯一权威时间源，供 Lease 获取/过期、verdict 与 Reveal_Event 顺序、Evidence 产生时间、Attestation 签发时间与有效期窗口、gate decision 时间使用，并保证同一 daemon 生命周期内对已提交事件单调不回退。
    - 客户端提供的时间戳只作为参考元数据记录，不参与 lease 过期与 verdict-before-reveal 判定；Evidence 保留窗口同样按该时钟计量。
    - **所有权**：新增 `rust_ext/src/daemon/clock.rs`。
    - **依赖门禁**：无；与 3.1 并行。
    - _Requirements: 14.11, 14.12_

  - [~] 3.9 实现 daemon 侧 Attestation 签发
    - 基于连接 Peer_Credential 派生的 Identity 与 Authoritative_Clock 签发 Attestation，绑定 Identity、记录标识（verdict_id 或 evidence_id）、View_Manifest hash、Contract_Hash 与有效期窗口。
    - 拒绝客户端自签 Attestation 作为授权输入；签发失败时返回 unknown/block，不接受客户端自签替代。
    - **所有权**：新增 `rust_ext/src/daemon/attestation.rs`。
    - **依赖门禁**：完成 3.3、3.8。
    - _Requirements: 14.13_

  - [~] 3.10 实现并发 gate 判定隔离
    - 每次 gate 判定各自绑定独立的 Gate_Snapshot、Current_Envelope 绑定与 Evidence 集合；任一方未提交的中间态不进入另一方的快照与结论。
    - 快照冲突（S1 ≠ S0）只影响冲突的那次判定，另一并发 gate 结论不受影响。
    - **所有权**：新增 `rust_ext/src/daemon/gate_session.rs`。
    - **依赖门禁**：完成 3.7。
    - _Requirements: 14.15_

  - [~] 3.11 将昂贵 verifier 执行移出 SQLite 写事务
    - verifier 在 daemon 进程内、SQLite 写事务之外执行；事务内只提交不可变记录与状态转换，使长耗时验证不占用写锁。
    - 快照内容 hash 只覆盖 Envelope relevant scope、Actual_Changes 与声明的 verifier 依赖，全仓库 hash 只作为非默认显式请求。
    - **所有权**：新增 `rust_ext/src/daemon/verifier_exec.rs`。
    - **依赖门禁**：完成 3.7。
    - _Requirements: 14.16, 6.18_

  - [~] 3.12 实现 Stage_Toggle 存储、作用域解析与前置阶段校验
    - 在 daemon 拥有的配置存储中持久化 P0–P4 各自的 Stage_Toggle，支持 global/workspace/task 三级作用域，并记录每次变更的 Peer_Identity 与 Authoritative_Clock 时间。
    - 同一配置存储还需承载 **Independence_Policy** 的取值（默认 `required`，非默认 `solo`）：同样记录每次变更的发起者 Peer_Identity 与 Authoritative_Clock 时间，同样禁止由单次请求参数或客户端自报字段设置。
    - **职责边界**：D0 只提供 Independence_Policy 的存储与变更审计，**不实现 P1 的政策语义**——「该 profile 是否要求 Independent_Review」的解析属于 P1 的 4.5，D0 不做任何 gate 判定。
    - 解析优先级为 task > workspace > global，缺值继承更宽作用域，全局默认关闭；P2/P3/P4 要求同一生效作用域 P1 已启用，P4 额外要求 P3 已启用；违规变更以 Structured_Reason 拒绝并保留变更前全部取值，不自动级联关闭高阶段。
    - 混合状态下只评估已启用阶段的条款；gate decision 与 Envelope revision 记录当时解析出的开关集合，开关集合变化后旧 gate decision 不可复用；P0 独立于 P1–P4，不以任何产品化阶段为前置。
    - **存储过渡与保值迁移**：daemon 配置存储首次可用时，把 `Experiment_Batch_Config` 中已记录的 P0 取值**按原作用域保值迁入**，不得重置为默认关闭；迁移动作本身记为一次带发起者与 Authoritative_Clock 时间的可审计 Stage_Toggle 变更。
    - 迁移完成后 daemon 配置存储是 P0–P4 三级 Stage_Toggle 的**唯一真相源**；`Experiment_Batch_Config` 中的残留取值只作审计历史，不参与解析（改写残留值不得改变解析结果）。两种存储下 P0 解析都不读取 P1–P4 开关。
    - **所有权**：新增 `rust_ext/src/daemon/stage_toggle.rs`、`server/stage_toggle_migration.py`（`Experiment_Batch_Config` → daemon 配置存储的保值迁移落点）；`rust_ext/src/daemon/config.rs`（仅 Stage_Toggle 存储接线）。
    - **依赖门禁**：完成 3.7、3.8；迁移源 `Experiment_Batch_Config` 的格式由 1.1 定义（wave 0，先于本 wave），本任务不因此依赖 G0。
    - _Requirements: 5.13, 13.11–13.21_

  - [~] 3.13 建立稳定错误码目录、双语 message key 与降级恢复指引
    - 建立已发布的错误码目录，使每个 Structured_Reason 携带一个稳定错误码和一个可在 `zh_CN` 与 `en_US` 两个 catalog 中解析的 i18n message key；文案变化不改变错误码。
    - 覆盖 D0 已知拒绝路径：SID 不匹配、Peer_Credential 不可获取、载荷尺寸/摘要不符、请求超时、Attestation 签发失败或越窗、Stage_Toggle 前置缺失、Degraded_Mode 下 Governance_Write 被拒。
    - Degraded_Mode 下 Governance_Write 被拒的 Structured_Reason 必须给出**该平台的具体 daemon 拉起命令与端点位置**作为可执行恢复指引，而不是泛化的"数据库正忙"。
    - 覆盖范围同时包含**警告码**：Requirement 7.15 的空 scope 发布警告码与其 i18n message key 一并登记在同一目录中。警告码非阻断（不改变操作的接受或拒绝语义），但同样要求码值稳定、`zh_CN` 与 `en_US` 均可解析，且文案变化不改变码值。
    - 跨类操作被拒时的 Structured_Reason 需要能表达"已执行组成部分 / 被拒组成部分"，错误码目录为该形态预留稳定码与 message key。
    - 覆盖范围同时包含 **Independence_Policy 相关码**：`high_risk` profile 拒绝 `solo` 的错误码，以及「独立审核按政策豁免」在 gate decision 与 CLI 输出中使用的稳定标识码；两者的 i18n message key 均须在 `zh_CN` 与 `en_US` 可解析，文案变化不改变码值。豁免标识码不得表述为「独立性已证明」。
    - 覆盖范围同时包含 **Attestation 撤销请求缺少 `Revocation_Mode`** 的错误码：该码用于 8.2/8.7 的拒绝路径，i18n message key 在 `zh_CN` 与 `en_US` 均可解析，文案变化不改变码值；message 必须提示显式指定 `compromised` 或 `rotated`，不得暗示系统会取默认值。
    - **所有权**：新增 `rust_ext/src/daemon/error_codes.rs`；`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 3.4、3.6、3.7、3.27。
    - _Requirements: 1.12, 5.14–5.15, 7.16, 10.12, 14.30, 14.36_

  - [~] 3.14 实现经 daemon 的 CLI 写命令面
    - Envelope 发布、verdict 提交（含封存）、Reveal、gate 触发均作为 CLI 写命令，经 Daemon_Endpoint 路由到串行化点执行；输出携带稳定错误码与本地化 message key。
    - 写命令不得绕过 daemon 直接开库；连不上端点时先走 3.25 的自动唤起，唤起失败后按 3.27/3.28 的 Degraded_Mode 分级——这些命令全部属于 Governance_Write，一律 fail closed 并输出含平台具体拉起命令的恢复指引，任务与步骤状态保持不变。
    - **所有权**：`cli/main.py`。
    - **依赖门禁**：完成 3.7、3.12、3.13、3.25、3.28。
    - _Requirements: 14.4, 14.17, 14.22, 14.30_

  - [~] 3.15 实现只读 MCP 查询工具面
    - Role_View 获取、Evidence 查询、Freshness_Status 查询与 gate decision 查询注册为只读 MCP 工具；只读工具不得触发任何写操作（含 workspace 激活一类隐式 UPDATE）。
    - 只读返回的 Freshness_Status 是查询时刻派生值，不构成 gate 结论。
    - **所有权**：`server/mcp_server.py`。
    - **依赖门禁**：完成 3.7、3.12、3.13。
    - _Requirements: 14.17_

  - [ ]* 3.16 编写属性测试：Property 13 串行化点唯一性
    - 生成任意并发 Protected_Mutation 序列，证明最终持久化顺序等价于经过 daemon 单一串行化点的某个全序，且不存在绕过该点成功提交的 Protected_Mutation；SQLite 写锁的获得与释放不改变授权、ownership、lease 与 Independent_Review 判定。
    - **Property 13: 串行化点唯一性（P-D）**
    - **Validates: Requirements 14.6, 14.7**
    - **所有权**：新增 `tests/test_property_daemon_serialization.py`。
    - **依赖门禁**：完成 3.7、3.14。

  - [ ]* 3.17 编写属性测试：Property 14 并发无锁失败
    - 多会话并发提交格式正确的读写请求，证明在请求超时内完成的请求都不返回数据库锁错误；超时请求返回结构化超时原因且任务状态不变。
    - **Property 14: 并发无锁失败（P-D）**
    - **Validates: Requirements 14.14**
    - **所有权**：新增 `tests/test_property_daemon_concurrency.py`。
    - **依赖门禁**：完成 3.14、3.15。

  - [ ]* 3.18 编写属性测试：Property 15 并发 Gate 快照隔离
    - 并发执行两路 gate 判定，证明结论只依赖各自的 Gate_Snapshot、Current_Envelope 绑定与 Evidence 集合，任一方未提交中间态都不改变另一方结论。
    - **Property 15: 并发 Gate 快照隔离（P-D）**
    - **Validates: Requirements 14.15**
    - **所有权**：新增 `tests/test_property_gate_snapshot_isolation.py`。
    - **依赖门禁**：完成 3.10。

  - [ ]* 3.19 编写属性测试：Property 16 权威时钟单调与唯一
    - 注入超前、滞后、乱序的客户端时间戳，证明 lease 时间字段、过期判定与 verdict-before-reveal 顺序只依赖 daemon 时钟，且已提交事件的时钟单调不回退；客户端时间戳只作为参考元数据被记录。
    - **Property 16: 权威时钟单调与唯一（P-D）**
    - **Validates: Requirements 14.11, 14.12**
    - **所有权**：新增 `tests/test_property_authoritative_clock.py`。
    - **依赖门禁**：完成 3.7、3.8。

  - [ ]* 3.20 编写属性测试：Property 17 对端身份不可伪造
    - 对任意客户端自报身份字段取值，证明 Peer_Identity 与授权结论只由 OS Peer_Credential 决定；macOS 缺 pid 时仍派生有效身份；Windows 每个 Unix UID ACL 校验点都有等价 SID 比较且不匹配即拒绝；客户端自签、issuer 非 daemon、绑定/签名失败或越窗的 Attestation 永不被接受为授权输入。
    - **Property 17: 对端身份不可伪造（P-D）**
    - **Validates: Requirements 14.5, 14.8, 14.9, 14.13**
    - **所有权**：新增 `tests/test_property_peer_identity.py`。
    - **依赖门禁**：完成 3.3、3.4、3.9。

  - [ ]* 3.21 编写属性测试：Property 22 阶段开关一致性
    - 生成任意 Stage_Toggle 变更序列，证明解析遵循 task > workspace > global 与缺值继承、全局默认关闭；不存在“P2/P3/P4 启用而同作用域 P1 关闭”或“P4 启用而 P3 关闭”的可达状态；违规变更被拒并保留原值；开关集合变化后旧 gate decision 不可复用；P0 解析不依赖 P1–P4。
    - **Property 22: 阶段开关一致性**
    - **Validates: Requirements 13.11, 13.12, 13.13, 13.14, 13.15, 13.16, 13.17**
    - **所有权**：新增 `tests/test_property_stage_toggle.py`。
    - **依赖门禁**：完成 3.12。

  - [ ]* 3.22 编写 D0 传输、载荷、SDDL 与错误码单元测试
    - 覆盖 Windows 命名管道端点建立与三平台 RPC 方法集等价性、`canonical_bytes_b64` 超尺寸与摘要不符被拒、每个 D0 失败路径的错误码稳定且在 `zh_CN` 与 `en_US` 均可解析（对应 Property 12）。
    - 增加 Windows 管道 SDDL 验收：安全描述符只授权 owner SID（可选 local administrators），以其他用户 SID 连接被拒并返回 Structured_Reason。
    - 增加端点实现负向验收：扫描实现与配置，断言不存在 Windows AF_UNIX 端点、监听 TCP 端口或本机 HTTPS 协同 RPC 入口。
    - **所有权**：新增 `tests/test_daemon_cross_platform_unit.py`。
    - **依赖门禁**：完成 3.1、3.6、3.13。
    - _Requirements: 14.1–14.4, 14.10, 14.18, 14.20, 14.21, 1.12_

  - [~] 3.23 同步 D0 平台、部署与阶段能力文档
    - 更新 daemon 平台支持矩阵（Linux/macOS/Windows 端点与传输能力）、CLI 写命令参考、只读 MCP 工具清单、部署与服务化指南（含 macOS launchd 与 Windows 服务托管）以及实施状态。
    - 记录 Windows 端点决策：命名管道、按 owner SID 派生的管道名与仅授权 owner SID 的 SDDL、多实例保活与 accept 竞态消除，以及排除 AF_UNIX / 监听 TCP / 本机 HTTPS 的理由（OS 不提供 Peer_Credential）。
    - 记录 daemon 不可用时的行为：自动唤起机制与有界等待窗口（默认 10 秒、可配置）、三平台唤起方式、单实例互斥，以及 Degraded_Mode 三类分流（只读与 Index_Write 直连、Governance_Write fail closed 并给出平台具体拉起命令）与 Degraded_Mode 标记的审计用途；说明降级产物因缺有效 Attestation 判 invalid，因此系统不设物理写屏障。
    - 按 Requirements 14.32、11.13 正面陈述 P4 Lease 边界：daemon 在线期间的并发正确性保证，防篡改归属 Attestation 与追加式 Evidence_Ledger，不得描述为能防止离线直接改库。
    - D0 未完成前 P1–P4 仍标记 planned/unavailable，且不得声明 P1 门禁可用。
    - **所有权**：`docs/deployment.md`、`docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`、`docs/design/implementation-status.md`。
    - **依赖门禁**：完成 3.1–3.15、3.24–3.29。
    - _Requirements: 13.1, 13.10, 11.13, 14.18–14.33_

  - [~] 3.24 新增 Windows 平台依赖并按 feature 裁剪
    - `rust_ext/Cargo.toml` 当前没有 `windows` / `windows-sys` 依赖，命名管道端点、对端令牌与安全描述符都需要平台 API；新增 `[target.'cfg(windows)'.dependencies]`，并按 feature 裁剪到 NamedPipe、Token、SecurityDescriptor 三类模块，不引入整包。
    - 写法与现有 `[target.'cfg(unix)'.dependencies] signal-hook` 对称：平台依赖只在对应 target 引入，Unix 侧构建不受影响。
    - **所有权**：`rust_ext/Cargo.toml`。
    - **依赖门禁**：无；这是 D0 的第一项实施任务，不依赖 G0。
    - _Requirements: 14.2, 14.5, 14.18_

  - [~] 3.25 实现 daemon 自动唤起与有界等待窗口
    - 客户端连不上 Daemon_Endpoint 时先尝试启动 daemon，并在有界等待窗口内以指数退避重试连接；窗口内任一次重试成功即在该连接上继续执行原请求，调用方不感知中断。窗口默认 10 秒、可配置，按**客户端时钟**计量——此时 daemon 尚未就绪，Authoritative_Clock 不存在。
    - 三平台唤起方式：Windows 启动分离进程（客户端进程退出后 daemon 仍存活）、macOS 经 launchd 激活已注册 user agent、Linux 经 systemd 用户级服务激活已注册单元。
    - 现状对照：当前**没有任何自动唤起逻辑**（`cli/main.py` 的 `run_agent_mode` → `_agent_start` 是前台 watcher 主循环，由外部管理器托管），因此这是新增能力，不是既有行为的重构。
    - **所有权**：新增 `server/daemon_autostart.py`。
    - **依赖门禁**：完成 3.2。
    - _Requirements: 14.22, 14.24, 14.25, 14.26_

  - [~] 3.26 实现唤起单实例跨进程互斥
    - 启动 daemon 前必须先取得跨进程互斥（Windows 命名互斥体、Linux/macOS 文件锁），保证同一用户 Daemon_Endpoint 上最多一个 daemon 进程；未取得互斥的会话不启动进程，只在有界等待窗口内继续退避重试。
    - 缺这道互斥时 N 个会话并发唤起会产生 N 个 daemon 进程，也就是 N 个串行化点，直接违反 Requirement 14.6；因此这是本级唯一的安全性要求，不得降级为"尽力去重"。
    - **所有权**：新增 `server/daemon_mutex.py`。
    - **依赖门禁**：完成 3.25。
    - _Requirements: 14.23, 14.6_

  - [~] 3.27 实现 Degraded_Mode 操作分类与分流策略
    - 实现 `class(op)` 分类：read_only（Role_View 获取、Evidence 查询、Freshness_Status 查询、gate decision 查询等）、Index_Write（文件刷新、解析结果、符号图更新、图刷新版本记录）、Governance_Write（等同 Protected_Mutation 全集：Envelope 发布、verdict 封存、Reveal_Event 追加、Evidence 追加、gate decision 提交、`task_apply`、`task_close`、Lease 获取/续租/释放）。
    - 定义分流策略：等待窗口耗尽仍未建连即进入 Degraded_Mode，read_only 直连只读连接执行、Index_Write 直连写入、Governance_Write fail closed 并返回带稳定错误码、i18n key 与恢复指引的 Structured_Reason，任务与步骤状态保持请求前状态。
    - `class(op)` 对同一操作恒定，不随 `degraded` 取值、重试次数或调用方变化，使分流结论可判定、可重放。
    - **跨类操作按组成部分分级**：实现 `components(op)` 拆分，使分级判定作用于**组成部分**而非整个入口；Degraded_Mode 下 Index_Write 组成部分直连执行、Governance_Write 组成部分 fail closed。新增用户可见入口一律按组成部分分类，不得因"入口只有一个"就整体归类。
    - **状态推进只挂在 Governance_Write 组成部分的成功路径上**：因此索引组成部分成功时，任务与步骤状态**从未被写入**（不是先写后回滚），step 不进入 `done`、task 不进入 `review`；该情形不产生任何 Evidence 与 gate decision，索引刷新成功不得被解释为门禁通过或条款满足。
    - 返回的 Structured_Reason 标识**已执行的组成部分集合与被拒的组成部分集合**，并携带稳定错误码、i18n key 与可执行恢复指引。
    - **所有权**：新增 `server/degraded_mode.py`。
    - **依赖门禁**：无；与 3.25 并行。
    - _Requirements: 14.27, 14.28, 14.29, 14.30, 14.34, 14.35, 14.36, 14.37_

  - [~] 3.28 在 daemon 客户端接线降级分流与 Degraded_Mode 标记
    - 把 3.25 的自动唤起与 3.27 的分流策略接入 `server/daemon_client.py`：连接失败先唤起，唤起失败按 `class(op)` 分流；只读降级在现有 `_sql_fallback_*` 方法与 `sql_fallbacks` 计数基线上扩展，不另建第二条回退路径。
    - Degraded_Mode 下执行的任何操作，都随产出记录或查询结果记录 Degraded_Mode 标记与降级原因，使审计能区分"经 daemon 路径产生"与"经降级直连路径产生"的记录。
    - **所有权**：`server/daemon_client.py`。
    - **依赖门禁**：完成 3.26、3.27。
    - _Requirements: 14.28, 14.29, 14.30, 14.33_

  - [~] 3.29 实现无有效 Attestation 记录的 invalid 判定，不设物理写屏障
    - 在 attestation 模块暴露可判定的有效性校验：无 daemon 签发 Attestation、issuer 非 daemon、绑定/签名校验失败或签发时间越窗的 verdict/Evidence 一律判为 invalid；Degraded_Mode 下直连 SQLite 写入的记录与绕过 CLI 直接开库写入的记录都落在这一类。
    - 判定结果由 3.28 的降级写入路径与 P1 Gate（4.4、4.5）消费，使这类记录不满足任何 Blocking_Clause；**不实现任何物理写屏障**——安全性由 Attestation 校验承担，而不是靠阻止别人开库。
    - **所有权**：`rust_ext/src/daemon/attestation.rs`。
    - **依赖门禁**：完成 3.9。
    - _Requirements: 14.31, 14.13, 10.8, 10.9_

  - [ ]* 3.30 编写属性测试：Property 25 唤起单实例性
    - 生成任意并发唤起序列（任意会话数、到达顺序与退避时序），证明同一用户 Daemon_Endpoint 上的 daemon 进程数 ≤ 1、串行化点数量恒为 1；未取得跨进程互斥的会话不启动进程，只在有界窗口内继续退避重试。
    - **Property 25: 唤起单实例性（P-D）**
    - **Validates: Requirements 14.22, 14.23**
    - **所有权**：新增 `tests/test_property_daemon_autostart_singleton.py`。
    - **依赖门禁**：完成 3.25、3.26。

  - [ ]* 3.31 编写属性测试：Property 26 降级分级确定性
    - 构造 daemon 不可用场景，证明 Degraded_Mode 下所有 `class(op) = governance_write` 请求失败且返回含稳定错误码、i18n key 与恢复指引的 Structured_Reason、任务与步骤状态等于请求前状态；所有 read_only 请求成功返回结果、所有 index_write 请求成功写入；`class(op)` 对同一操作恒定。
    - **Property 26: 降级分级确定性（P-D）**
    - **Validates: Requirements 14.27, 14.28, 14.29, 14.30**
    - **所有权**：新增 `tests/test_property_degraded_mode_grading.py`。
    - **依赖门禁**：完成 3.27、3.28。

  - [ ]* 3.32 编写属性测试：Property 27 无 Attestation 记录不可过门禁
    - 生成 Degraded_Mode 直连写入与绕过 CLI 直接开库写入的 verdict/Evidence，证明 `att(r)` 无效即判 invalid 且不满足任何 Blocking_Clause；结论不依赖任何物理写屏障，且降级产生的记录都带 Degraded_Mode 标记与降级原因，可被审计与 daemon 路径记录区分。
    - **Property 27: 无 Attestation 记录不可过门禁（P-D）**
    - **Validates: Requirements 14.31, 14.33**
    - **所有权**：新增 `tests/test_property_unattested_records.py`。
    - **依赖门禁**：完成 3.28、3.29。

  - [ ]* 3.33 编写属性测试：Property 28 端点可连续性
    - 断言监听期间始终维持 ≥2 个管道实例且在服务每个已接受连接**之前**补建替换实例；在两次 accept 之间高频发起合法客户端连接，证明不返回 pipe-busy 或端点缺失错误。
    - **Property 28: 端点可连续性（P-D）**
    - **Validates: Requirements 14.19**
    - **所有权**：新增 `tests/test_property_named_pipe_continuity.py`。
    - **依赖门禁**：完成 3.1、3.2。

  - [ ]* 3.34 编写属性测试：Property 30 Stage_Toggle 存储迁移保值
    - daemon 配置存储不可用时，生成任意 global/workspace/task 三级作用域的 P0 取值序列，断言取值写入 `Experiment_Batch_Config`、无 schema 变更，且每次变更记录发起者 session marker 与客户端时钟时间。
    - daemon 配置存储可用后触发迁移，断言取值保持（未被重置为默认关闭）、作用域保持，且迁移事件带发起者与 Authoritative_Clock 时间。
    - 断言迁移后解析只读 daemon 存储：改写 `Experiment_Batch_Config` 中的残留值不改变解析结果；并断言两种存储下 P0 解析都不读取 P1–P4 开关。
    - **Property 30: Stage_Toggle 存储迁移保值**
    - **Validates: Requirements 13.18, 13.19, 13.20, 13.21**
    - **所有权**：新增 `tests/test_property_stage_toggle_migration.py`。
    - **依赖门禁**：完成 3.12；迁移源格式由 1.1 定义（wave 0）。

- [ ] 4. P1：实现版本化 Envelope、角色投影、blind verdict、snapshot Evidence 与双门禁
  - [~] 4.1 新增 P1 schema 与幂等迁移
    - 新增不可变 `task_contract_revisions`、`task_role_view_events`、`task_verdict_events`、`task_evidence_events`、`task_gate_decisions` 及必要索引/约束；为旧库提供顺序、事务化、可重复迁移。
    - 新增 Verifier_Registry 结构（name、version、config_hash、trust_status、注册时间）与保留窗口/归档元数据（默认 365 天、归档位置、按标识符可解析）；归档只搬迁不改写 payload。
    - 新增 `Verifier_Revocation_Record` 存储结构：字段含 Verifier 三元组 `(name, version, config_hash)`、撤销原因、发起者身份，以及按 Authoritative_Clock 记录的撤销时间；该记录不可变、只追加，同一三元组的一次撤销只对应一条记录。
    - 明确**不**为撤销建立"逐条失效事件"的批量写入路径：schema 与迁移层不提供按历史 evidence 批量写入失效事件的通道，撤销导致的 `invalid` 由查询层按三元组匹配派生（见 4.4）。个体失效事件表仍保留，供 Requirement 6.6 的单条 evidence 失效使用。
    - 不回填猜测的历史绑定；旧 `test_runs` 保持原数据并由查询层派生 `historical_unbound`。
    - **所有权**：`db/schema.py`、`db/db_base.py`。
    - **依赖门禁**：G0 与 GD 均通过。
    - _Requirements: 1.7, 2.6–2.9, 4.3–4.6, 6.1, 6.3, 6.6, 6.11–6.13, 6.16–6.17, 6.20, 6.23, 7.2–7.3, 8.4–8.5, 13.10_

  - [~] 4.2 实现 Canonical Envelope、profile 校验、revision 发布与 hash
    - 实现 parser/printer、UTF-8 稳定序列化、路径与数组规范化、纯展示字段排除、单调 revision、语义变更升版及 declarative/executable clause 分类。
    - 实现空 Allowed_Edit_Scope 三分支：`code_change`/`high_risk` 空 scope 拒绝发布并保留上一已接受 revision；`research`/`design`/`review` 记为 `unscoped`；无 target 的存量任务记为 `scope_migration_pending` 并要求先发布带显式 file 或 symbol scope 的迁移 revision。
    - **空 scope 发布期防呆**：`research`/`design`/`review` 发布时若派生 scope 为空集，**发布照样成功**（7.12 的接受语义不变，Envelope 仍记为 `unscoped`），同时返回一条**非阻断** Structured_Warning，说明 scope 为 `unscoped`、任何磁盘文件改动都会让后续 Completion_Gate 与 Apply_Gate 阻断该任务，并提示在 task step 上声明 `target_file` 或 `target_symbol` 以建立显式 scope。
    - 该 Structured_Warning 携带一个来自已发布码目录的稳定**警告码**与一个在 `zh_CN` 与 `en_US` 均可解析的 i18n message key；文案变化不改变警告码。警告不改变操作的接受或拒绝语义，不得因此拒绝发布。
    - 对 grammar/profile/verifier/freshness/revision/hash 异常返回结构化错误且 fail closed。
    - **所有权**：新增 `db/db_task_contracts.py`。
    - **依赖门禁**：完成 4.1。
    - _Requirements: 1.1, 2.1–2.11, 5.4, 7.4, 7.9, 7.11–7.16_

  - [~] 4.3 实现 Role View allowlist、blind verdict、reveal 与 amendment
    - 从同一 Contract Hash 投影 Planner/Implementer/Reviewer/Tester view，递归拒绝未 allowlist 的嵌套值，并生成 view manifest hash。
    - allowlist 由 `(view_type, view_version, 披露阶段)` 版本化定义唯一标识，是递归披露判定的唯一真相源；条目增删改必须升 view_version，View_Manifest 记录 allowlist 定义 hash；引用未注册 view_version 时拒绝生成 Role View 并把绑定该 view 的 verdict 判为 invalid。
    - 首轮 verdict 封存前禁止 Implementer Notes、既有 verdict/draft/confidence/review focus；封存后追加 reveal，修订只能追加 amendment，保存结构化决定而非思维链。
    - **所有权**：新增 `db/db_task_reviews.py`。
    - **依赖门禁**：完成 4.2。
    - _Requirements: 1.4–1.5, 3.1–3.11, 4.1–4.8, 5.1–5.5, 13.9_

  - [~] 4.4 实现规范化 Workspace Snapshot、append-only Evidence 与 freshness
    - 快照覆盖 HEAD、规范化 dirty diff、相关 tracked/untracked 内容 hash，且只覆盖 relevant scope、Actual_Changes 与声明的 verifier 依赖；Evidence 绑定契约、快照、file/symbol/graph/test/verifier/producer/payload。
    - 实现 fresh/stale/invalid/superseded/historical_unbound 派生与全序优先级 `invalid > superseded > stale > fresh`，Structured_Reason 同时报告所选状态与生效优先级；契约 revision 前进使旧绑定 evidence 至少为 `superseded`。
    - 接入 Verifier_Registry：无条目或 `trust_status ≠ trusted` 使相关 evidence 判为 invalid。
    - **撤销采用单条记录 + 查询时派生**：`trust_status` 置为 `revoked` 时只追加**一条** `Verifier_Revocation_Record`，不对历史 evidence 逐条写入失效事件；`invalid` 在**查询时**由该记录与 evidence 的 Verifier 三元组 `(name, version, config_hash)` 匹配派生。
    - 派生必须确定：同一 evidence 与同一撤销记录集合，重复派生结果恒定；撤销派生出的 `invalid` 服从全序优先级 `invalid > superseded > stale > fresh`；撤销**不修改任何既有 payload**，既有 evidence 记录逐字节保留。
    - 个体失效（payload 校验失败、引用不存在等）仍按 Requirement 6.6 追加个体失效事件；被取消的只是"撤销 Verifier 时必须逐条写事件"，不是失效事件机制本身。
    - Evidence 追加与 gate decision 提交经 daemon 串行化点并使用 Authoritative_Clock；保留窗口按该时钟计量，归档逐字节保留 payload。
    - **所有权**：新增 `db/db_task_evidence.py`、`db/task_snapshot.py`。
    - **依赖门禁**：完成 4.2。
    - _Requirements: 1.2, 1.7, 6.1–6.24, 7.6–7.8_

  - [~] 4.5 实现统一 Evidence Gate 判定内核与 Profile_Policy_Matrix 查表
    - 统一评估契约绑定、blocking clauses、verdict、scope、finding、test/static evidence、freshness 与 profile；`unsatisfied/unknown/stale/invalid` 一律阻断。
    - 实现 Profile_Policy_Matrix：profile 必需的 sealed Reviewer blind verdict、独立 Tester verdict 与当前快照 Evidence 一律查表解析，不由 gate 自行推断；`high_risk` 要求 Reviewer、Tester、Implementer 为三个不同 Session；profile 不在表中时返回 Structured_Reason 并拒绝评估。
    - **"是否要求 Independent_Review"由 Profile_Policy_Matrix 与生效 Independence_Policy 共同解析**：`solo` 生效时该 profile **不要求** Independent_Review，因此 Requirement 1.5 的前件为假；`high_risk` 在任何情况下拒绝 `solo` 并保留原政策取值。政策取值只从 daemon 配置存储读取（3.12 提供存储与变更审计），无记录时默认 `required`；请求参数与客户端自报字段不参与解析。
    - `solo` 生效时 gate decision 记录「独立审核按政策豁免」与当时的政策取值，**不得**表述为「独立性已证明」；相关 verdict 仍保留 Requirement 5.2 赋予的 `unproven_independence` 标记。
    - `solo` **不放宽** Requirement 1.4 的封存/reveal 顺序、Requirement 1.6 的 scope 封闭性、Requirement 1.8 的每个 Blocking_Clause Evidence freshness。
    - 政策由 `solo` 改回 `required` 后，`solo` 期间产生的 gate decision 一律不可复用，受影响任务必须在当前政策下重新评估（与 Stage_Toggle 集合变化的处理一致）。
    - **gate decision 记录 Verifier 三元组与判定时间**：每次 decision 必须记录其所用每条 evidence 的 Verifier 三元组 `(name, version, config_hash)` 与按 Authoritative_Clock 记录的判定时间，使「该 evidence 在那次判定时刻是否已被撤销」可通过与撤销记录的撤销时间比较事后重算，不依赖历史失效事件。
    - 在 verifier 外捕获 S0、状态转换前捕获同输入集 S1；捕获失败为 unknown，变化为 stale，并追加 decision/event，不静默 PASS。
    - **所有权**：新增 `db/db_task_gate.py`。
    - **依赖门禁**：完成 4.3、4.4。
    - _Requirements: 1.1–1.9, 4.6–4.7, 5.2–5.12, 5.14–5.17, 6.9–6.10, 6.14–6.15, 6.19, 6.22, 8.3–8.5, 8.11_

  - [~] 4.6 将 P1 Mixin 接入 CodeGraphDB
    - 注册 contract/review/evidence/gate Mixin，明确调用方向，避免新建平行状态机；保证旧数据库在未启用 P1 时行为兼容。
    - **所有权**：`db/db.py`。
    - **依赖门禁**：完成 4.2–4.5。
    - _Requirements: 13.1–13.5_

  - [~] 4.7 将新测试运行绑定到 Evidence，并隔离历史 PASS
    - 扩展 JUnit 导入/适配接口，使新 run 具有唯一 run ID、selectors、contract/snapshot/verifier 绑定并追加 Evidence；旧记录只返回 `historical_unbound`，不得反向推断。
    - **所有权**：`db/db_tests.py`。
    - **依赖门禁**：完成 4.4。
    - _Requirements: 1.3, 6.4–6.5, 6.11–6.12, 7.1–7.3_

  - [~] 4.8 接入 `task_report_step` Completion Gate 并复用现有 completion review
    - 从 step target 聚合 Envelope scope，以磁盘 diff/hash 为权威，复用 change audit、`task_symbol_changes`、`_check_scope_violations`、check gate 与 `run_task_completion_review`。
    - `unscoped` 任务的 scope 比较判为 `not_applicable`，仅当 Actual_Changes 为空才算在 scope 内；`scope_migration_pending` 任务在迁移 revision 之前的改动不参与越界判定。
    - 对 provided interface 的 scope 外 callers，将验证责任写入显式集成任务；仅当 caller 已在当前 scope、`high_risk` 政策要求或兼容性无法维持时留在当前任务，禁止隐式扩大局部修改范围。
    - block/stale/unknown 时采用更严格结果，保持或设置 blocked 并创建修复步骤/finding；Evidence ledger 与可清理 operational finding 分离。
    - **Degraded_Mode 行为**：`task_report_step` 是已知跨类操作实例——Index_Write 组成部分是刷新文件状态与 `task_symbol_changes` 记录，Governance_Write 组成部分是运行 Completion_Gate 判定与追加 Evidence/提交 decision；按 3.27 的 `components(op)` 拆分分流，不按整个入口归类。
    - Degraded_Mode 下索引组成部分执行、门禁与 Evidence 组成部分被拒：step **不得进入 `done`**、task **不得进入 `review`**，任务与步骤状态等于请求前状态，且不产生任何 Evidence 与 gate decision；返回的 Structured_Reason 标识已执行与被拒的组成部分。
    - daemon 恢复后重放同一入口时，Index_Write 组成部分必须**幂等**（任意次数重复执行后的索引状态等于单次执行结果），Governance_Write 组成部分在 daemon 串行化点与 Authoritative_Clock 下执行。
    - **所有权**：`db/db_tasks.py`（仅 completion/report 路径）、`db/db_task_quality.py`（仅 Evidence 引用、严格结果与 caller 集成任务适配）。
    - **依赖门禁**：完成 4.5–4.7；Degraded_Mode 分流策略来自 3.27、3.28（D0，更早 wave）。
    - _Requirements: 1.2, 1.6, 6.8–6.10, 7.4–7.14, 8.1–8.2, 8.11, 13.2–13.5, 14.34–14.39_

  - [~] 4.9 接入 `task_apply` 主门禁、Reopen 与父级聚合，保持 `task_close` 仅收尾
    - 叶子 apply 必须处于 review、绑定当前 Envelope、具有 sealed blind verdict/profile verdict、fresh satisfied evidence、无 open blocker、manifest 一致且 S0=S1；失败保留原状态并返回逐条原因。
    - `scope_migration_pending` 期间 `task_apply` 一律以 Structured_Reason 拒绝并保持请求前状态。
    - 父级联必须检查子任务仍有效 gate decision 和父级 clauses；缺陷或 stale 只经现有 Reopen 返回 `in_progress`。`task_close` 只允许 applied→closed/现有级联，不添加替代正确性 gate。
    - **所有权**：`db/db_tasks.py`（仅 apply/reopen/parent cascade/close 路径）。
    - **依赖门禁**：完成 4.8。
    - _Requirements: 1.5, 1.8–1.9, 7.13–7.14, 8.3–8.11, 13.2–13.5_
  - [~] 4.10 暴露 P1 CLI 与本地化结构化输出
    - 增加 contract publish/show、role view、blind verdict、reveal/amend、evidence status、gate explain 命令；扩展 task report/apply 参数但保持 close 只收尾。
    - 写命令（发布、verdict、reveal、gate 触发）经 daemon 串行化点执行；所有失败输出 contract/snapshot/clause/scope/independence reason code 与可解析 message key，禁止输出隐藏推理历史。
    - 4.2 的空 scope 发布 Structured_Warning 必须同时出现在**发布返回值与 CLI 输出**两处，使 Agent 调用方拿到结构化字段、人类操作者在终端可见；警告码稳定、i18n message key 在 `zh_CN` 与 `en_US` 均可解析，且警告不改变命令的成功退出语义。
    - 提供查看与设置 **Independence_Policy** 的命令：写命令经 daemon 落到 3.12 的配置存储（不接受单次请求参数自我豁免），读命令输出当前生效取值与来源作用域。
    - 输出必须明确区分「按政策豁免通过」与「独立性已证明」两种结论，不得混用同一文案；`high_risk` profile 请求 `solo` 时输出带稳定错误码与 i18n key 的拒绝原因，并说明原政策取值已保留。
    - **所有权**：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 4.6–4.9。
    - _Requirements: 1.12, 2.1–2.11, 3.1–3.11, 4.1–4.8, 5.13–5.15, 6.1–6.19, 7.15–7.17, 8.1–8.11, 13.9–13.10, 14.17_

  - [~] 4.11 暴露 P1 MCP 工具
    - 为 P1 contract/view/verdict/reveal/evidence/gate 查询注册只读薄包装器；Role_View、Evidence、Freshness_Status 与 gate decision 查询不触发任何写操作。
    - 更新现有 task_report_step/task_apply 工具参数，mutation 复用 DB 层校验并经 daemon 路径，task_close 不接主门禁。
    - **所有权**：`server/mcp_server.py`。
    - **依赖门禁**：完成 4.6–4.9。
    - _Requirements: 3.1–3.11, 4.1–4.8, 6.1–6.19, 8.1–8.11, 13.2–13.5, 13.10, 14.17_

  - [ ]* 4.12 编写 P1 schema 升降级边界与幂等迁移测试
    - 覆盖旧库升级、重复迁移、事务失败回滚、不可变记录约束、Verifier_Registry 与归档元数据结构，以及历史 test_runs 不被伪绑定。
    - **所有权**：新增 `tests/test_multi_llm_contract_p1_migration.py`。
    - **依赖门禁**：完成 4.1。
    - _Requirements: 1.7, 6.3, 6.11–6.13, 6.16–6.17, 7.2–7.3, 13.10_

  - [ ]* 4.13 编写 Envelope/profile/canonicalization 单元测试
    - 覆盖 parser 错误位置、打印稳定性、round trip、semantic/presentation 变更、revision 单调性和 executable clause 必填字段。
    - 覆盖空 scope 三分支：`code_change`/`high_risk` 拒绝发布并保留上一 revision、`research`/`design`/`review` 记为 `unscoped`、无 target 存量任务记为 `scope_migration_pending`（对应 Property 20）。
    - 覆盖空 scope 发布警告：`research`、`design`、`review` 三个 profile 的空 scope 发布**都成功**且返回 Structured_Warning；警告码稳定；i18n message key 在 `zh_CN` 与 `en_US` 两个 catalog 中均可解析；警告同时出现在发布返回值与 CLI 输出两处；接受语义未变（Envelope 仍记为 `unscoped`，7.12 判定不受影响）。
    - **所有权**：新增 `tests/test_multi_llm_contract_envelope.py`。
    - **依赖门禁**：完成 4.2、4.10（CLI 输出断言需要发布命令已接线）。
    - _Requirements: 2.1–2.11, 5.4, 7.11–7.17_

  - [ ]* 4.14 编写 Role View 与 blind/reveal 单元测试
    - 递归生成嵌套/metadata/artifact reference 泄露样本，验证 allowlist、manifest 一致性、封存顺序、amendment 追加和无思维链字段。
    - 验证 allowlist 条目变更必须升 view_version、View_Manifest 记录 allowlist 定义 hash、引用未注册 view_version 时拒绝生成且绑定 verdict 判为 invalid（对应 Property 21）。
    - **所有权**：新增 `tests/test_multi_llm_contract_reviews.py`。
    - **依赖门禁**：完成 4.3。
    - _Requirements: 3.1–3.11, 4.1–4.8, 5.1–5.5_

  - [ ]* 4.15 编写 Snapshot/Evidence/freshness 单元测试
    - 覆盖 dirty/untracked 内容、各绑定维度变化、graph refresh、追加 invalidation、S0/S1 同输入集与捕获失败。
    - 覆盖 `invalid > superseded > stale > fresh` 全序优先级与优先级报告、`superseded` 产生条件、Verifier_Registry 未注册/非 trusted 的 invalid 传播、保留窗口按权威时钟计量与归档逐字节一致（对应 Property 18、23）。
    - 覆盖 Verifier_Registry **撤销**（对应改写后的 Property 19）：撤销后账本中**只有一条** `Verifier_Revocation_Record`、**未产生** N 条失效事件、该三元组的历史 evidence 在查询时派生为 `invalid`、既有 payload 逐字节不变、同输入重复派生结果恒定；并断言按某次 gate decision 记录的 Verifier 三元组与判定时间，可判定该 evidence 在当时是否已被撤销。
    - 覆盖个体失效仍走事件：payload 校验失败等单条 evidence 失效按 Requirement 6.6 追加个体失效事件，与撤销派生路径互不干扰。
    - **所有权**：新增 `tests/test_multi_llm_contract_evidence.py`。
    - **依赖门禁**：完成 4.4、4.5。
    - _Requirements: 6.1–6.24, 7.1–7.3_

  - [ ]* 4.16 编写 P1 完整成功路径集成测试
    - 自动化验证 `work_next_job → task_report_step → review → blind verdict → reveal/amend → task_apply → task_close`；确认 close 只在 applied 后收尾。
    - **所有权**：新增 `tests/test_multi_llm_contract_p1_flow.py`。
    - **依赖门禁**：完成 4.7–4.11。
    - _Requirements: 3.3–3.6, 4.1–4.8, 7.4–7.10, 8.1–8.6, 13.2–13.5_

  - [ ]* 4.17 编写 P1 失败、TOCTOU、父级与 Reopen 集成测试
    - 覆盖报告后改文件、历史 PASS、未自报越界 diff、signature/caller 集成任务、同会话、父级 blocker、gate 中 workspace 变化、checker 故障及新 revision 恢复。
    - 覆盖 verifier 撤销传播后 apply 被拒，并断言账本**只增加一条**撤销记录、**未产生**逐条失效事件；覆盖新 revision 使旧 evidence 因 `superseded`（而非 `stale`）被拒、`scope_migration_pending` 任务 apply 被拒并保持状态。
    - **所有权**：新增 `tests/test_multi_llm_contract_p1_gate_failures.py`。
    - **依赖门禁**：完成 4.7–4.11。
    - _Requirements: 1.1–1.9, 5.1–5.11, 6.9–6.17, 6.20–6.21, 6.23, 7.2, 7.6–7.14, 8.2–8.11_

  - [ ]* 4.18 编写属性测试：Property 1 契约唯一性
    - 对任意当前 Envelope 与随机错配 id/revision/hash，证明错配 verdict/Evidence 不能满足条款并保持状态不变。
    - **Property 1: 契约唯一性**
    - **Validates: Requirements 1.1**
    - **所有权**：新增 `tests/test_property_contract_binding.py`。
    - **依赖门禁**：完成 4.2、4.5。

  - [ ]* 4.19 编写属性测试：Property 2 当前快照性
    - 对 snapshot/file/symbol hash 任一随机变更，证明 Evidence 不再 fresh。
    - **Property 2: 当前快照性**
    - **Validates: Requirements 1.2**
    - **所有权**：新增 `tests/test_property_snapshot_freshness.py`。
    - **依赖门禁**：完成 4.4、4.5。

  - [ ]* 4.20 编写属性测试：Property 3 测试当前性
    - 生成 selector、run status、binding、verifier 与后续变更组合，验证 `test_pass` 当且仅当全部必要条件成立。
    - **Property 3: 测试当前性**
    - **Validates: Requirements 1.3**
    - **所有权**：新增 `tests/test_property_current_test_pass.py`。
    - **依赖门禁**：完成 4.5、4.7。

  - [ ]* 4.21 编写属性测试：Property 4 盲评顺序
    - 生成 verdict/reveal 时间和嵌套 manifest 字段，证明 reveal 后 verdict 或含禁止字段的 view 永远无效；时间顺序判定使用 Authoritative_Clock。
    - **Property 4: 盲评顺序**
    - **Validates: Requirements 1.4**
    - **所有权**：新增 `tests/test_property_blind_ordering.py`。
    - **依赖门禁**：完成 4.3、4.5。

  - [ ]* 4.22 编写属性测试：Property 5 独立性
    - 在 P1 可用 session marker 范围内生成相同/不同/缺失 marker，证明无法证明不同会话时 apply 必须失败；P3 将强化 Identity 证明。
    - **Property 5: 独立性**
    - **Validates: Requirements 1.5**
    - **所有权**：新增 `tests/test_property_review_independence.py`。
    - **依赖门禁**：完成 4.3、4.9。

  - [ ]* 4.23 编写属性测试：Property 6 范围封闭
    - 生成 allowed files/symbols 与 actual diff/hash 集合，证明任一越界至少被 completion/apply gate 阻断；空 scope 在任何分支都不产生"任意改动均通过"。
    - **Property 6: 范围封闭**
    - **Validates: Requirements 1.6**
    - **所有权**：新增 `tests/test_property_scope_closure.py`。
    - **依赖门禁**：完成 4.8、4.9。

  - [ ]* 4.24 编写属性测试：Property 7 追加性
    - 对任意 Evidence/verdict payload 追加 rerun、amendment、invalidation 后，逐字节验证旧 payload 未更新或删除；归档路径同样不修改既有 payload。
    - **Property 7: 追加性**
    - **Validates: Requirements 1.7**
    - **所有权**：新增 `tests/test_property_append_only_ledger.py`。
    - **依赖门禁**：完成 4.3、4.4。

  - [ ]* 4.25 编写属性测试：Property 8 门禁完备性
    - 生成 blocking clause 的 satisfied/unsatisfied/unknown/stale/invalid/superseded 组合，证明仅全 fresh satisfied 可 applied。
    - **Property 8: 门禁完备性**
    - **Validates: Requirements 1.8**
    - **所有权**：新增 `tests/test_property_gate_completeness.py`。
    - **依赖门禁**：完成 4.5、4.9。

  - [ ]* 4.26 编写属性测试：Property 9 Reopen 可恢复性
    - 生成失败记录、新 revision 与新 snapshot Evidence，证明新记录可重新评估且旧失败审计仍存在。
    - **Property 9: Reopen 可恢复性**
    - **Validates: Requirements 1.9**
    - **所有权**：新增 `tests/test_property_reopen_recovery.py`。
    - **依赖门禁**：完成 4.4、4.9。

  - [~] 4.27 同步 P1 CLI/MCP/架构/状态文档
    - 记录命令与工具参数、schema 版本与迁移、Envelope/View/Verdict/Evidence/Gate 语义、Verifier_Registry 与撤销传播、`superseded` 优先级、allowlist 版本化、保留与归档、历史 PASS 限制、Reopen、父级聚合及 `task_close` 仅收尾；P2–P4 仍标记 unavailable。
    - 记录 P1 写路径经 daemon 串行化点与 Authoritative_Clock（Requirement 14.17），并同步因新增 MCP/Mixin/schema 引起的项目指标与实施状态，避免能力超前声明。
    - **所有权**：`docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`、`docs/design/implementation-status.md`、`README.md`、`AGENTS.md`、`CONTRIBUTING.md`。
    - **依赖门禁**：完成 4.1–4.11。
    - _Requirements: 13.1–13.10, 14.17_

  - [ ]* 4.28 编写属性测试：Property 29 跨类操作组成部分隔离
    - 在 Degraded_Mode 下调用 `task_report_step`，断言索引组成部分（文件状态 + `task_symbol_changes`）成功、门禁判定与 Evidence 追加组成部分被拒。
    - 断言 step 状态与 task 状态**等于请求前状态**（step 不进入 `done`、task 不进入 `review`），且不产生任何 Evidence 记录与 gate decision；返回的 Structured_Reason 标识已执行与被拒的组成部分并携带稳定错误码、i18n key 与恢复指引。
    - daemon 恢复后重放同一入口，断言 Index_Write 组成部分幂等（任意次数重复执行后的索引状态等于单次执行结果），Governance_Write 组成部分在 daemon 串行化点与 Authoritative_Clock 下执行。
    - 断言分级结论只由 `class(part)` 决定，与整个入口无关。
    - **Property 29: 跨类操作组成部分隔离**
    - **Validates: Requirements 14.34, 14.35, 14.36, 14.37, 14.38**
    - **所有权**：新增 `tests/test_property_mixed_class_operation.py`。
    - **依赖门禁**：完成 3.27、3.28、4.8。

  - [ ]* 4.29 编写大量引用下的 Verifier 撤销集成测试
    - 构造一个被大量 Evidence 引用的 verifier（同一 `(name, version, config_hash)` 三元组产出多条 evidence），撤销后断言 `task_apply` 被拒并保持请求前状态。
    - 断言账本**只增加一条** `Verifier_Revocation_Record`、**未产生**逐条失效事件，且既有 evidence payload 逐字节不变。
    - 按撤销**之前**某次 gate decision 记录的 Verifier 三元组与 Authoritative_Clock 判定时间，断言该次判定时刻该 evidence 尚未被撤销（撤销时间晚于判定时间），证明时点可重算不依赖历史失效事件。
    - **所有权**：新增 `tests/test_multi_llm_contract_verifier_revocation_scale.py`。
    - **依赖门禁**：完成 4.4、4.5、4.9、4.10。
    - _Requirements: 6.13, 6.20, 6.21, 6.22, 6.23_

  - [ ]* 4.30 编写属性测试：Property 31 独立性豁免的范围与可审计性
    - 生成任意 profile、Independence_Policy 取值与 session marker 组合，证明 `solo` 生效时该 profile **不要求** Independent_Review（Requirement 1.5 前件为假），而 Requirement 5.2 对 `unproven_independence` 的标记语义不变；不存在任何政策取值使**未证明**的独立性满足要求独立审核的条款。
    - 证明 `high_risk` 在任何情况下不接受 `solo`：请求被以 Structured_Reason 拒绝且原政策取值保留。
    - 证明 `solo` 不改变 Requirement 1.4（封存/reveal 顺序）、1.6（scope 封闭性）、1.8（Blocking_Clause Evidence freshness）的判定结果。
    - 证明生效 `policy` 只能取自 daemon 配置存储（无记录时默认 `required`）：任意请求参数与客户端自报字段都不改变生效取值，且每次变更都有带发起者 Peer_Identity 与 Authoritative_Clock 时间的可审计记录。
    - 证明 `solo` 期间产生的 gate decision 在政策改回 `required` 后不可复用。
    - **Property 31: 独立性豁免的范围与可审计性**
    - **Validates: Requirements 5.12, 5.13, 5.14, 5.15, 5.16, 5.17**
    - **所有权**：新增 `tests/test_property_independence_policy.py`。
    - **依赖门禁**：完成 3.12、4.5、4.9。

  - [ ]* 4.31 编写 `solo` 政策端到端集成测试
    - 置为 `solo` 后由**单个 Session** 完成实现与盲评并成功 `task_apply`；断言 gate decision 记录「独立审核按政策豁免」与当时政策取值、verdict 仍带 `unproven_independence`、Requirement 1.4/1.6/1.8 未被放宽（封存顺序、scope 封闭性、Evidence freshness 断言全部照旧生效）。
    - 政策改回 `required` 后断言 `solo` 期间的 gate decision 不被复用、受影响任务需重新评估。
    - 断言 `high_risk` profile 请求 `solo` 被拒并保留原取值。
    - **所有权**：新增 `tests/test_multi_llm_contract_solo_policy_flow.py`。
    - **依赖门禁**：完成 4.9、4.10。
    - _Requirements: 5.12–5.17_

- [~] 5. G1 检查点：P1 全部自动化验证通过后才进入 P2
  - Ensure all tests pass, ask the user if questions arise.
  - 核对 `task_report_step` 和 `task_apply` 使用同一 freshness/gate 语义，`task_close` 未新增正确性判断，且 assignment/lease 仍不可用。
- [ ] 6. P2：实现 artifact/interface 依赖、provider 解析与环检测
  - [~] 6.1 新增 P2 依赖与 interface schema 及幂等迁移
    - 持久化四类依赖、artifact identity/freshness、interface identity/version/hash 与显式 provider 选择；迁移失败原子保留旧 revision/graph。
    - **所有权**：`db/schema.py`、`db/db_base.py`。
    - **依赖门禁**：G1 通过。
    - _Requirements: 9.1–9.9, 13.7, 13.10_

  - [~] 6.2 实现依赖解析、边归一化与最小 cycle path
    - `requires_existing` 只验证存在性；仅显式 artifact 和已解析 interface 形成 provider→consumer hard edge，去重后检测环；多 provider 无 Planner 选择立即拒绝。
    - informational relationship 不阻断；只实现无环校验和诊断，不实现资源优化、自动 assignment 或复杂 DAG 调度。
    - **所有权**：新增 `db/db_task_dependencies.py`。
    - **依赖门禁**：完成 6.1。
    - _Requirements: 1.10, 9.1–9.10, 13.6–13.8_

  - [~] 6.3 将 P2 Dependency Mixin 接入 CodeGraphDB
    - 注册依赖模块并复用 P1 Envelope 发布路径，不创建独立调度状态机。
    - **所有权**：`db/db.py`。
    - **依赖门禁**：完成 6.2。
    - _Requirements: 9.1–9.10, 13.5_

  - [~] 6.4 将 artifact/interface freshness 与环检测接入发布和 Gate
    - 发布 revision 前原子构图并拒绝 hard cycle；consumer 条款仅在 provider artifact fresh、interface identity/version/hash 匹配时满足；caller 验证默认转交显式集成任务。
    - **所有权**：`db/db_task_contracts.py`（依赖字段发布校验）、`db/db_task_gate.py`（依赖 freshness 判定）。
    - **依赖门禁**：完成 6.2、6.3。
    - _Requirements: 1.10, 7.10, 9.2–9.9_

  - [~] 6.5 暴露 P2 CLI 与本地化诊断
    - 提供 dependency/interface inspect、provider select、cycle explain 命令；明确没有自动排程、assignment 或抢占。
    - **所有权**：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 6.3、6.4。
    - _Requirements: 1.12, 9.1–9.10, 13.7–13.8_

  - [~] 6.6 暴露 P2 MCP 工具
    - 注册依赖查询、provider 选择、cycle diagnostics 薄包装器，并复用 DB 层原子发布校验。
    - **所有权**：`server/mcp_server.py`。
    - **依赖门禁**：完成 6.3、6.4。
    - _Requirements: 9.1–9.10, 13.10_

  - [ ]* 6.7 编写 P2 schema 与迁移测试
    - 覆盖旧库升级、重复迁移、失败回滚、旧 revision/graph 保留和多 provider 约束。
    - **所有权**：新增 `tests/test_multi_llm_contract_p2_migration.py`。
    - **依赖门禁**：完成 6.1。
    - _Requirements: 9.4–9.7, 9.9, 13.10_

  - [ ]* 6.8 编写 P2 依赖与接口集成测试
    - 覆盖四类关系、边方向/去重、artifact stale、interface hash/version、caller 集成任务、informational edge 和无隐式 provider。
    - **所有权**：新增 `tests/test_multi_llm_contract_p2_dependencies.py`。
    - **依赖门禁**：完成 6.2–6.6。
    - _Requirements: 9.1–9.10_

  - [ ]* 6.9 编写属性测试：Property 10 依赖无环性
    - 生成任意有向依赖图，证明每个被接受 hard graph 无环；有环 revision 被原子拒绝并返回实际 cycle path。
    - **Property 10: 依赖无环性（P2）**
    - **Validates: Requirements 1.10**
    - **所有权**：新增 `tests/test_property_dependency_acyclicity.py`。
    - **依赖门禁**：完成 6.2、6.4。

  - [~] 6.10 同步 P2 CLI/MCP/架构/状态文档
    - 记录四类依赖、provider 选择、freshness、cycle diagnostics 与 caller 集成任务边界；明确不含复杂 DAG 调度、自动 assignment 和中央调度器。
    - **所有权**：`docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`、`docs/design/implementation-status.md`、`README.md`。
    - **依赖门禁**：完成 6.1–6.6。
    - _Requirements: 9.1–9.10, 13.6–13.10_

- [~] 7. G2 检查点：P2 全部自动化验证通过后才进入 P3
  - Ensure all tests pass, ask the user if questions arise.
  - 确认 hard graph 无环且只提供校验/诊断，没有 assignment、自动 dispatch、资源优化或复杂 DAG scheduler。

- [ ] 8. P3：实现 agent/session/model Identity 与独立审核证明
  - [x] 8.1 新增 P3 Identity/attestation schema 与幂等迁移
    - 为 contract/view/verdict/evidence/gate/state action 记录 agent_id/session_id/model_id/role 与 attestation 元数据（含 issuer 标识、签名密钥标识、绑定字段、按 Authoritative_Clock 记录的 Attestation 签发时间、有效期窗口与撤销状态）；缺失身份不得由 reviewer 自由文本或 ownership 补齐。
    - 新增 `Attestation_Revocation_Record` 存储结构：字段含 Attestation issuer 标识、签名密钥标识、`Revocation_Mode`、撤销原因、发起者身份，以及按 Authoritative_Clock 记录的撤销时间；该记录不可变、只追加，同一 issuer/签名密钥的一次撤销只对应一条记录。
    - 明确**不**为 issuer/签名密钥撤销建立"逐条失效事件"的批量写入路径：schema 与迁移层不提供按历史 verdict/Evidence 批量写入失效事件的通道，撤销导致的 `invalid` 由查询层按 issuer 标识与签名密钥标识匹配、并结合 `Revocation_Mode` 与 Attestation 签发时间派生（见 8.2）。个体失效事件表仍保留，供 Requirement 6.6 的单条记录失效使用。
    - **所有权**：`db/schema.py`、`db/db_base.py`。
    - **依赖门禁**：G2 通过。
    - _Requirements: 10.1–10.18, 13.10_

  - [x] 8.2 实现 Identity 验证、策略与 Attestation 校验
    - 校验 action identity 完整性/唯一性、session 分离、可配置 agent/model-family 分离；返回结构化 identity reason，身份仅作 actor attribution。
    - Attestation 必须由 daemon 签发（Requirement 14.13）、绑定 Peer_Identity 派生的 Identity、记录标识、View_Manifest hash 与 Contract_Hash，且签发时间落在有效期窗口内；客户端自签、issuer 非 daemon、绑定/签名失败或越窗一律 fail closed 并把关联 verdict/Evidence 判为 invalid。
    - **issuer/签名密钥撤销采用单条记录 + 查询时派生**：撤销只向账本追加**一条** `Attestation_Revocation_Record`，不对历史 verdict/Evidence 逐条写入失效事件；以该 issuer 标识 + 签名密钥标识为唯一 Attestation 的记录，其 `invalid` 在**查询时**按匹配派生。
    - **`Revocation_Mode` 必填且无默认值**：撤销请求未携带 `Revocation_Mode` 时以 Structured_Reason 拒绝并**不追加任何撤销记录**，`compromised` 与 `rotated` 都不得作为该请求的隐式默认值。
    - **`compromised` 忽略签发时间**：匹配该 issuer/签名密钥的每条记录一律派生 `invalid`，结论与该记录的 Attestation 签发时间无关。
    - **`rotated` 只影响轮换之后**：仅当记录的 Attestation 签发时间**晚于**撤销时间才派生 `invalid`；签发时间早于或等于撤销时间的记录保持原有有效性判定——这一条是例行密钥轮换不会把轮换前的历史账本整体判死的原因。
    - 派生必须确定：同一记录与同一撤销记录集合，重复派生结果恒定；撤销派生出的结论就是 Requirement 10.9 的那个 `invalid`，不引入第二个状态值。
    - 撤销**不修改任何既有 payload**，既有 verdict/Evidence 记录逐字节保留；记录因自身原因失效（Attestation 绑定校验失败、payload 校验失败）时仍按 Requirement 6.6 追加个体失效事件，被取消的只是"撤销 issuer/key 时必须逐条写事件"。
    - **所有权**：新增 `db/db_task_identity.py`。
    - **依赖门禁**：完成 8.1。
    - _Requirements: 1.5, 10.1–10.18_

  - [x] 8.3 将 P3 Identity Mixin 接入 CodeGraphDB
    - 注册身份模块，不把 `active_task_id`、assignment metadata 或 SQLite lock 当成身份/授权证明。
    - **所有权**：`db/db.py`。
    - **依赖门禁**：完成 8.2。
    - _Requirements: 10.5, 10.7, 13.4–13.5_

  - [x] 8.4 强化 blind view/verdict/reveal 的独立审核证明
    - 证明 allowlisted manifest、verdict-before-reveal（按 Authoritative_Clock 定序）、有效 daemon 签发 attestation 与 Reviewer/Implementer session 不同；high_risk 按政策验证独立 Tester、不同 agent/model family。
    - **所有权**：`db/db_task_reviews.py`。
    - **依赖门禁**：完成 8.2、8.3。
    - _Requirements: 1.4–1.5, 10.1–10.5, 10.8_

  - [x] 8.5 将 Identity fail-closed 规则接入 Evidence Gate
    - 缺失/不完整/不唯一/attestation 失败时排除 actor attribution 与 verdict clause satisfaction；apply session 必须不同于 Implementer session。
    - Attestation 越窗、自签、issuer 不符或被撤销时把关联 verdict/Evidence 判为 invalid 并保持请求前状态；撤销派生按 `Revocation_Mode` 的模式语义执行——`compromised` 命中匹配 issuer/签名密钥的全部记录且与签发时间无关，`rotated` 仅命中签发时间晚于撤销时间的记录。
    - **gate decision 记录 issuer 标识、签名密钥标识与 Attestation 签发时间**：每次 decision 必须记录其所用每条 verdict/Evidence 的 issuer 标识、签名密钥标识与 Attestation 签发时间，以及按 Authoritative_Clock 记录的判定时间，使「该记录在那次判定时刻是否已被撤销」可通过与匹配撤销记录的撤销时间和 `Revocation_Mode` 比较事后重算，不依赖历史失效事件；此处与 4.5 已要求的 Verifier 三元组记录对称（Requirement 6.22）。
    - **所有权**：`db/db_task_gate.py`。
    - **依赖门禁**：完成 8.4。
    - _Requirements: 1.5, 10.2–10.18_

  - [x] 8.6 将身份上下文接入 task mutation 与审计
    - task report/apply/reopen/close 接收已验证 Identity；apply 强制不同 session，close 仍只收尾；不引入 assignment/lease 权限。
    - **所有权**：`db/db_tasks.py`。
    - **依赖门禁**：完成 8.5。
    - _Requirements: 10.1, 10.5–10.7, 13.3–13.5_

  - [x] 8.7 暴露 P3 CLI 与本地化 identity reason
    - 扩展相关命令输入/输出 agent/session/model/role/attestation，拒绝自由文本 reviewer 冒充证明，并说明 Attestation 只能由 daemon 签发。
    - issuer/签名密钥撤销命令必须**显式**接收 `Revocation_Mode`（`compromised` 或 `rotated`），CLI 侧不施加任何默认值；未携带该值的请求以带稳定错误码与可在 `zh_CN`、`en_US` 两个 catalog 中解析的 i18n message key 的 Structured_Reason 拒绝，且不追加任何撤销记录。
    - **所有权**：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 8.3–8.6。
    - _Requirements: 1.12, 10.1–10.18_

  - [x] 8.8 暴露 P3 MCP Identity 接口
    - 扩展 contract/view/verdict/evidence/gate/task 工具传递可验证调用身份，包装层不得伪造缺省身份或代客户端签发 Attestation。
    - 撤销相关接口必须透传 `Revocation_Mode` 且不补默认值，缺该值时按 8.2 的 Structured_Reason 拒绝；只读查询返回的撤销派生 `invalid` 是查询时刻按模式语义计算的派生值，包装层不得缓存为持久状态或代为写入逐条失效事件。
    - **所有权**：`server/mcp_server.py`。
    - **依赖门禁**：完成 8.3–8.6。
    - _Requirements: 10.1–10.18, 13.10_

  - [ ]* 8.9 编写 P3 schema 与迁移测试
    - 覆盖旧记录可读、Identity 新记录必填、attestation 绑定字段与撤销状态、重复迁移、失败回滚和无自由文本回填。
    - 覆盖 `Attestation_Revocation_Record` 结构：issuer 标识、签名密钥标识、`Revocation_Mode`（必填、无默认值、取值限于 `compromised`/`rotated`）、撤销原因、发起者身份与按 Authoritative_Clock 记录的撤销时间齐备且不可变、只追加；并断言 schema 与迁移层**不存在**按历史 verdict/Evidence 批量写入逐条失效事件的通道，而个体失效事件表仍保留供 Requirement 6.6 使用。
    - **所有权**：新增 `tests/test_multi_llm_contract_p3_migration.py`。
    - **依赖门禁**：完成 8.1。
    - _Requirements: 10.1, 10.5, 10.8–10.12, 10.17, 10.18, 13.10_

  - [ ]* 8.10 编写 P3 身份与独立审核集成测试
    - 覆盖同 session、缺字段、重复主体、无效 attestation、high_risk agent/model-family/Tester 政策、apply session 分离及 active_task_id 无授权效果。
    - 覆盖客户端自签 Attestation 被拒、越窗签发被判 invalid。
    - 覆盖 issuer/签名密钥撤销：撤销后断言账本**只增加一条** `Attestation_Revocation_Record`、**未产生**任何逐条失效事件；`compromised` 模式下匹配该 issuer/密钥的全部记录判为 `invalid` 且结论与 Attestation 签发时间无关；`rotated` 模式下只有签发时间晚于撤销时间的记录判为 `invalid`，签发时间早于或等于撤销时间的记录有效性判定不变；缺少 `Revocation_Mode` 的撤销请求被拒且账本不增加任何记录；两种模式下既有 verdict/Evidence payload 逐字节不变。
    - **所有权**：新增 `tests/test_multi_llm_contract_p3_identity.py`。
    - **依赖门禁**：完成 8.2–8.8。
    - _Requirements: 1.4–1.5, 10.1–10.18, 13.4_

  - [x] 8.11 同步 P3 CLI/MCP/架构/状态文档
    - 记录 Identity、daemon 签发 Attestation 校验、撤销传播、独立审核政策与 fail-closed reason；明确 Identity 不等于 assignment、lease、ownership 或 SQLite lock，P4 仍 unavailable。
    - 记录 Attestation 撤销的两种模式：`compromised` 忽略签发时间、命中匹配 issuer/签名密钥的全部记录；`rotated` 仅命中签发时间晚于撤销时间的记录，因此例行密钥轮换不会把轮换前的历史账本判死。同时明确 `Revocation_Mode` 必填且无默认值，缺该值的请求被拒。
    - 明确说明撤销**不写入逐条失效事件**：账本只增加一条 `Attestation_Revocation_Record`，`invalid` 由查询时派生；时点可重算性由 gate decision 记录的 issuer 标识、签名密钥标识、Attestation 签发时间与权威时钟判定时间保证；既有 payload 逐字节不变，个体失效仍按 Requirement 6.6 追加事件。
    - **所有权**：`docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`、`docs/design/implementation-status.md`、`README.md`。
    - **依赖门禁**：完成 8.1–8.8。
    - _Requirements: 10.1–10.18, 13.1, 13.4, 13.8–13.10_

  - [ ]* 8.12 编写属性测试：Property 32 Attestation 撤销模式语义与时点可重算
    - 生成任意 `Attestation_Revocation_Record` 集合、任意 `Revocation_Mode` 取值、任意 Attestation 签发时间与撤销时间组合，证明每次 issuer 或签名密钥撤销只追加**一条**不可变撤销记录，且**不产生** N 条逐记录失效事件。
    - 证明不携带 `Revocation_Mode` 的撤销请求以 Structured_Reason 被拒绝、账本不增加任何记录，且 `compromised` 与 `rotated` 都不被当作该请求的隐式默认值。
    - 证明 `amode(x) = compromised` 时匹配 issuer 标识与签名密钥标识的记录一律派生 `invalid`，该结论独立于该记录的 Attestation 签发时间。
    - 证明 `amode(x) = rotated` 时记录派生为 `invalid` **当且仅当**签发时间晚于撤销时间；签发时间早于或等于撤销时间的记录既有有效性判定保持不变。
    - 证明派生确定性与单一状态值：同一记录与同一撤销记录集合重复派生结果恒定，且撤销派生出的结论与 Requirement 10.9 的 Attestation 校验失败使用同一个 `invalid`，不存在第二个状态值。
    - 证明时点可判定：给定某次 gate decision 记录的 issuer 标识、签名密钥标识、Attestation 签发时间与权威时钟判定时间，"该记录在那次判定时刻是否已被撤销判为 `invalid`"可仅由匹配撤销记录的撤销时间与 `Revocation_Mode` 判定，不依赖任何历史失效事件。
    - 证明撤销不修改任何既有 verdict/Evidence 记录，既有 payload 逐字节不变；记录因自身原因失效时仍按 Requirement 6.6 追加个体失效事件。
    - **Property 32: Attestation 撤销模式语义与时点可重算**
    - **Validates: Requirements 10.10, 10.11, 10.12, 10.13, 10.14, 10.15, 10.16, 10.17, 10.18**
    - **所有权**：新增 `tests/test_property_attestation_revocation.py`。
    - **依赖门禁**：完成 8.2、8.5。

  - [ ]* 8.13 编写密钥轮换不误伤历史的端到端集成测试
    - 以同一组历史 verdict/Evidence 作为 fixture：先执行 `rotated` 撤销，断言轮换前签发的记录仍然有效、`task_apply` 结论不因轮换改变。
    - 在同一 fixture 上执行 `compromised` 撤销，断言这些记录全部判为 `invalid`、`task_apply` 被拒并保持请求前状态。
    - 对比两次 `task_apply` 结论必须不同，并断言两种情形下账本各自只增加了**一条** `Attestation_Revocation_Record`、均未产生逐条失效事件。
    - **所有权**：新增 `tests/test_multi_llm_contract_attestation_rotation.py`。
    - **依赖门禁**：完成 8.2–8.8。
    - _Requirements: 10.10–10.18_

- [~] 9. G3 检查点：P3 全部自动化验证通过后才进入 P4
  - Ensure all tests pass, ask the user if questions arise.
  - 确认所有要求独立审核的路径都能证明 session/attestation，Attestation 均由 daemon 签发，且尚未把 identity 或 active_task_id 当作 assignment/lease。
  - 确认 Attestation 撤销采用**单条记录 + 查询时派生**且派生随 `Revocation_Mode` 分模式：`Revocation_Mode` 必填无默认值、缺该值的请求被拒且不追加记录；`compromised` 忽略签发时间、`rotated` 只命中签发时间晚于撤销时间的记录，因此例行密钥轮换不会把轮换前的历史账本判为 `invalid`；账本未出现逐条失效事件，既有 payload 逐字节不变，且时点撤销状态可由 gate decision 记录的 issuer 标识、签名密钥标识、Attestation 签发时间与权威时钟判定时间重算。
- [x] 10. P4：实现 assignment 与 token/expiry/renew/release/fencing 安全 lease
  - [x] 10.1 新增 P4 assignment/lease/event schema 与幂等迁移
    - 记录 task+role+holder assignment，以及 lease_id、token hash、由权威时钟写入的 acquired/expires/renewed/released 时间、单调 fencing counter 和追加审计事件；永不存 raw token。
    - 唯一性/事务约束保证同 task+role 只有一个当前 lease，历史 lease 与 release event 可审计；claimed_by/claimed_at 不迁移成安全授权。
    - **所有权**：`db/schema.py`、`db/db_base.py`。
    - **依赖门禁**：G3 通过。
    - _Requirements: 1.11, 11.1–11.12, 13.4, 13.8, 13.10_

  - [x] 10.2 实现 assignment、lease 生命周期与 fencing 验证
    - acquire 原子比较当前状态并递增 counter；renew 要求当前 token/holder/counter 且未过期；release 追加事件并幂等。
    - `acquired_at`/`expires_at`/`renewed_at`/`released_at` 与过期判定一律读取 daemon Authoritative_Clock（Requirements 11.2、11.4、11.9 引用 14.11），客户端时间戳只作参考元数据（14.12），不参与过期判定。
    - protected mutation 验证 token hash、expiry、role、Identity 与当前 fencing，并在 daemon 唯一串行化点应用（Requirements 11.10、14.6）；旧 token/counter 即使旧进程复活也拒绝。
    - **所有权**：新增 `db/db_task_leases.py`。
    - **依赖门禁**：完成 10.1。
    - _Requirements: 1.11, 11.1–11.10, 14.6, 14.11, 14.12_
    - **官方计划记录（2026-08-03）**：LeaseMixin 生命周期与 fencing 已实现；
      权威时钟修复由 T-1785767529977-473fe88f 完成——`_clock()` 改读 daemon
      Authoritative_Clock（ping timestamp，Req 14.11），daemon 不可用时 fail
      closed（E_LEASE_CLOCK_UNAVAILABLE + error.governance_write_degraded 双语键
      + recovery_guidance，Req 14.30/1.12），不再回退客户端时钟；验证
      `pytest tests/test_p4_authoritative_clock.py tests/test_p4_lease_smoke.py`
      30 passed。

  - [x] 10.3 将 P4 Lease Mixin 接入 CodeGraphDB
    - 注册 assignment/lease 模块，明确 SQLite 写锁只负责短事务互斥，不提供业务 ownership；Protected_Mutation 的全序由 daemon 串行化点保证。
    - **所有权**：`db/db.py`。
    - **依赖门禁**：完成 10.2。
    - _Requirements: 11.10, 13.5, 14.6, 14.7_

  - [x] 10.4 将 lease/fencing 接入 protected task mutation
    - 为受保护的 contract/view/verdict/evidence/gate/task 写操作要求 task+role 对应 token 与当前 fencing；过期、token 不匹配、旧 counter 均在写入前拒绝且不改变 task data。
    - 全部 Protected_Mutation 经 daemon 串行化点应用，不暴露第二个串行化点；assignment/lease 不得绕过角色权限、Independent Review 或 Evidence Gate；`task_close` 仍只在 applied 后收尾。
    - **所有权**：`db/db_tasks.py`、`db/db_task_contracts.py`、`db/db_task_reviews.py`、`db/db_task_evidence.py`。
    - **依赖门禁**：完成 10.2、10.3。
    - _Requirements: 1.11, 11.1, 11.8–11.12, 13.2–13.5, 14.6, 14.7_
    - **官方计划记录（2026-08-03）**：受保护写入口接入 token/fencing 校验，
      失败返回 E_LEASE_* 且不改变 task/step data（Req 11.8-11.9）；验证
      test_p4_lease_smoke.py::test_protected_report_step_* 全绿。

  - [x] 10.5 暴露 P4 CLI 与本地化 lease reason
    - 提供 assignment create/show 与 lease acquire/renew/release/status 命令；raw token 仅在 acquire 成功响应安全返回一次，日志/数据库/错误不得泄露。
    - protected mutation 接受 token/fencing，输出 expiry/token/fencing/role/gate 的结构化拒绝原因；不实现自动 dispatch、抢占或中央调度。
    - 面向用户的 lease 文案按 Requirements 14.32、11.13 正面陈述边界：Lease 保证 daemon 在线期间的并发正确性，防篡改归属 Attestation 校验与追加式 Evidence_Ledger，且不得描述为能防止离线直接改库。
    - **所有权**：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json`。
    - **依赖门禁**：完成 10.3、10.4。
    - _Requirements: 1.12, 11.1–11.13, 13.6, 13.8, 14.31, 14.32_
    - **官方计划记录（2026-08-03）**：`cw lease acquire/renew/release/status/list`
      与 `cw assignment create/show/revoke` 已提供；raw token 仅 acquire 响应
      返回一次；结构化拒绝原因含稳定错误码 + 双语 message_key（Req 1.12）；
      `cw lease --help` / `cw assignment --help` 验证通过。

  - [x] 10.6 暴露 P4 MCP assignment/lease 工具
    - 注册 acquire/renew/release/status 与 assignment 工具，并为 protected mutation 透传 token/fencing；包装层不得记录 raw token 或放宽 DB 校验。
    - **所有权**：`server/mcp_server.py`。
    - **依赖门禁**：完成 10.3、10.4。
    - _Requirements: 11.1–11.12, 13.10_

  - [x] 10.7 编写 P4 schema 与迁移测试
    - 覆盖旧库升级、重复迁移、token hash/raw token 禁止、单调 counter、唯一当前 lease、事件追加和 claimed metadata 不获授权。
    - **所有权**：新增 `tests/test_multi_llm_contract_p4_migration.py`。
    - **依赖门禁**：完成 10.1。
    - _Requirements: 11.1–11.3, 11.6–11.7, 11.12, 13.10_

  - [x] 10.8 编写 lease 生命周期、幂等与权威时钟单元测试
    - 覆盖 acquire 竞争、expiry、renew/release 重放、token/holder/counter 不匹配、raw token 脱敏，以及注入超前/滞后/乱序客户端时间戳不改变过期判定的权威时钟边界。
    - **所有权**：新增 `tests/test_multi_llm_contract_p4_lease.py`。
    - **依赖门禁**：完成 10.2。
    - _Requirements: 11.2–11.9, 14.11, 14.12_
    - **官方计划记录（2026-08-03）**：`tests/test_multi_llm_contract_p4_lease.py`
      （并行 agent）与 `tests/test_p4_authoritative_clock.py`（本批次新增）覆盖
      acquire 竞争/expiry/renew-release 幂等/凭证不匹配/raw token 脱敏/权威时钟
      边界；daemon 不可用 fail closed 且无状态变更。注：前者的生命周期用例依赖
      daemon 权威时钟注入，时钟适配归该文件所有者（T-1785767529977-473fe88f
      白名单外）。

  - [x] 10.9 编写 P4 protected mutation 与 Gate 组合集成测试
    - 模拟旧持有者复活、并发 acquire、过期 lease、角色越权、有效 lease 但 Evidence Gate 失败，以及 SQLite 获锁但 lease 无效；全部必须无 task data 变更。
    - **所有权**：新增 `tests/test_multi_llm_contract_p4_protected_mutation.py`。
    - **依赖门禁**：完成 10.2–10.6。
    - _Requirements: 1.11, 11.8–11.11, 14.6, 14.7_

  - [x] 10.10 编写属性测试：Property 11 fencing 安全性
    - 生成任意 lease 获取/续租/释放/重新获取序列，证明新 counter N 发布后所有小于 N 的 protected mutation 永远被拒绝。
    - **Property 11: fencing 安全性（P4）**
    - **Validates: Requirements 1.11**
    - **所有权**：新增 `tests/test_property_lease_fencing.py`。
    - **依赖门禁**：完成 10.2、10.4。
    - **官方计划记录（2026-08-03）**：`tests/test_property_lease_fencing.py`
      （并行 agent）与 `tests/test_p4_lease_smoke.py::test_fencing_property_random_sequences`
      覆盖 Property 11；本批次新增确定性 fencing 用例
      `test_p4_authoritative_clock.py::test_fencing_stale_counter_rejected_under_daemon_clock`
      全绿。注：前者生命周期用例依赖 daemon 权威时钟注入，时钟适配归文件所有者。

  - [x] 10.11 同步 P4 CLI/MCP/架构/状态文档
    - 记录 assignment 与 lease 分层、token 安全、expiry/renew/release/fencing、protected mutation、daemon 串行化点与 SQLite lock 边界，以及 Evidence Gate 不可绕过。
    - 按 Requirements 14.32、11.13 正面陈述 P4 Lease 边界：Lease 是 daemon 在线期间的并发正确性保证；防篡改归属 Attestation 校验（14.31）与追加式 Evidence_Ledger；不得描述为能防止离线直接改库。同时记录 Degraded_Mode 下 Lease 获取/续租/释放属 Governance_Write，一律 fail closed。
    - 明确不提供复杂中央调度、自动 assignment、抢占、通用 Jira、实时聊天或共享推理历史，并同步 MCP/Mixin/schema 指标。
    - **所有权**：`docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`、`docs/design/implementation-status.md`、`README.md`、`AGENTS.md`、`CONTRIBUTING.md`。
    - **依赖门禁**：完成 10.1–10.6。
    - _Requirements: 11.1–11.13, 13.4–13.10, 14.30, 14.31, 14.32_

- [x] 11. G4 最终检查点：完成全阶段回归与范围核对
  - Ensure all tests pass, ask the user if questions arise.
  - 运行迁移、属性、单元、CLI/MCP 与跨阶段集成测试，并运行完整 daemon 回归 `cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`；确认 P0 记录未被当作 P1 Evidence，`task_close` 仍仅收尾，lease 未绕过 Identity/role/Gate，且代码、CLI 输出与文档均按 Requirements 14.32、11.13 正面陈述 Lease 边界（在线并发正确性 + 防篡改归 Attestation 与账本追加性），无"防离线改库"类表述。

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP；执行时仍应优先保留全部 hard-gate 属性测试。
- P1 的开始条件不是“P0 与 D0 代码完成”，而是 G0 对**真实、冻结协议实验批次**输出 `eligible_for_p1=true`，且 GD 的每一项交付物通过自动化验收；dependency graph 的 wave 顺序不能替代这两个条件判断。
- D0 与 P0 相互独立（Requirement 13.17）：D0 不阻塞 P0 批次，P0 也不是 D0 的前置；但 P1 必须同时满足 G0 与 GD。
- Windows 传输已决策为命名管道（Requirements 14.2、14.18–14.21）：按 owner SID 派生管道名、SDDL 仅授权 owner SID、≥2 实例且服务前补建；AF_UNIX、监听 TCP 与本机 HTTPS 一律排除，判据是 OS 是否提供不可伪造的对端身份。
- daemon 不可用已决策为"先唤起 + 按操作分级"（Requirements 14.22–14.33）：自动唤起（有界窗口默认 10 秒、退避重试、跨进程互斥保单实例）→ 唤起失败进入 Degraded_Mode → 只读与 Index_Write 允许直连、Governance_Write 一律 fail closed 并给出可执行恢复指引 → 降级产物因缺有效 Attestation 判 invalid，因此不设物理写屏障。
- P4 Lease 的边界按 Requirements 14.32、11.13 正面陈述：daemon 在线期间的并发正确性保证；防篡改归属 Attestation 与追加式 Evidence_Ledger；不得描述为能防止离线直接改库。
- 所有阶段必须复用现有任务状态机和 `work_next_job`、target fields、change audit、`task_symbol_changes`、completion review、`task_report_step`、`task_apply`、`task_close`、Reopen；禁止平行状态机。
- 本计划明确排除复杂中央调度、自动 dispatch/抢占、通用 Jira、实时 Agent 聊天、共享隐藏推理历史、任意自然语言证明，以及用 LLM verdict 替代确定性 verifier。
- 属性测试任务覆盖设计文档的 Property 1–11（核心不变量）、Property 13–17（D0 daemon 基座）、Property 22（阶段开关）、Property 25–30（唤起单实例性、降级分级确定性、无 Attestation 不可过门禁、端点可连续性、跨类操作组成部分隔离、Stage_Toggle 存储迁移保值）、Property 31（独立性豁免的范围与可审计性，由 4.30 覆盖）与 Property 32（Attestation 撤销模式语义与时点可重算，由 8.12 覆盖）；Property 12、18–21、23、24 由对应单元与集成任务覆盖（错误码目录与双语解析、freshness 优先级、Verifier 撤销、空 scope 三分支、allowlist 版本化、保留与归档、P0 schema 中立）。Windows 管道 SDDL 与端点负向约束由 3.22 的单元/负向验收覆盖；空 scope 发布警告由 4.13 覆盖。单元测试负责示例和错误边界，集成测试负责状态机接线，两者互补。
- Verifier 撤销采用**单条记录 + 查询时派生**（Requirements 6.13、6.20–6.24）：撤销只向账本追加一条 `Verifier_Revocation_Record`，`invalid` 由该记录与 evidence 的 Verifier 三元组在查询时匹配派生，不物化 N 条失效事件；时点可重算性由 gate decision 记录的三元组与 Authoritative_Clock 判定时间保证；既有 payload 逐字节不变，个体失效仍按 Requirement 6.6 追加事件。相关断言分布在 4.15（单元）、4.17 与 4.29（集成）。
- Attestation 撤销同样采用**单条记录 + 查询时派生**（Requirements 10.10–10.18），但派生多一个维度：撤销只向账本追加一条 `Attestation_Revocation_Record`，不物化逐条失效事件；`Revocation_Mode` 必填且**无默认值**，缺该值的请求以 Structured_Reason 被拒且不追加任何记录；`compromised` 忽略 Attestation 签发时间、命中匹配 issuer/签名密钥的全部记录，`rotated` 仅命中签发时间晚于撤销时间的记录，因此例行密钥轮换不会把轮换前的历史账本判死。时点可重算性由 gate decision 记录的 issuer 标识、签名密钥标识、Attestation 签发时间与 Authoritative_Clock 判定时间保证（与 Requirement 6.22 的 Verifier 侧对称）；既有 payload 逐字节不变，个体失效仍按 Requirement 6.6 追加事件。相关断言分布在 8.9（单元/迁移）、8.10（集成）、8.12（属性）、8.13（集成）。
- 独立审核豁免落在**前件**（Requirements 5.12–5.17）：`solo` 使该 profile 不再**要求** Independent_Review，因此 Property 5 的前件为假、属性本身仍成立；豁免不改变「未证明的独立性能否满足条款」（Requirement 5.2 不变），也不放宽 1.4/1.6/1.8。存储与变更审计在 D0 的 3.12，政策语义在 P1 的 4.5，CLI 在 4.10，验证在 4.30（属性）与 4.31（集成）。
- 共享文件在 P0 与 D0 之间由 wave 顺序串行化：`cli/main.py`、`i18n/zh_CN.json`、`i18n/en_US.json` 同时被 1.4（P0 CLI，wave 3）与 3.13（D0 错误码目录，wave 11）、3.14（D0 写命令面，wave 12）修改，依赖图已把它们放在不同 wave，因此不会同 wave 并行编辑。"D0 与 P0 相互独立"只指阶段门禁互不阻塞（G0 与 GD 相互独立），不表示可以真并行编辑这些共享文件。

## Task Dependency Graph

```json
{
  "gates": [
    {"id": "G0", "afterWave": 4, "beforeWave": 15, "condition": "P0 real batch eligible_for_p1=true, no unresolved gray zone, no pause condition"},
    {"id": "GD", "afterWave": 14, "beforeWave": 15, "condition": "D0 cross-platform daemon deliverables all pass automated acceptance, including named pipe SDDL and instance keep-alive, endpoint negative constraints, three-platform auto-start with single-instance mutex, Degraded_Mode grading with recovery guidance, component-level grading of Mixed_Class_Operation (index component executes, governance component fails closed, no state advance, no Evidence or gate decision), Stage_Toggle storage transition with value-preserving migration, daemon config store holding both Stage_Toggle and Independence_Policy values with auditable change records (policy semantics land in P1 and are accepted with 4.5), and unattested-record invalidation"},
    {"id": "G1", "afterWave": 23, "beforeWave": 24, "condition": "P1 migration, API, property and integration validation pass"},
    {"id": "G2", "afterWave": 29, "beforeWave": 30, "condition": "P2 dependency validation and cycle diagnostics pass"},
    {"id": "G3", "afterWave": 37, "beforeWave": 38, "condition": "P3 identity and independent-review proof validation pass, Attestation revocation uses a single immutable Attestation_Revocation_Record with mode-dependent query-time derivation (Revocation_Mode mandatory with no default, compromised ignores issuance time, rotated only invalidates records issued after the revocation time) so that routine key rotation does not invalidate the historical ledger, no per-record invalidation events are written, existing payloads stay byte-for-byte unchanged, and point-in-time revocation status is recomputable from the issuer identifier, signing key identifier, Attestation issuance time and Authoritative_Clock decision time recorded on each gate decision"}
  ],
  "waves": [
    {"id": 0, "tasks": ["1.1"]},
    {"id": 1, "tasks": ["1.2"]},
    {"id": 2, "tasks": ["1.3"]},
    {"id": 3, "tasks": ["1.4", "1.5", "1.7"]},
    {"id": 4, "tasks": ["1.6", "1.8"]},
    {"id": 5, "tasks": ["3.8", "3.24"]},
    {"id": 6, "tasks": ["3.1"]},
    {"id": 7, "tasks": ["3.2", "3.3"]},
    {"id": 8, "tasks": ["3.4", "3.6"]},
    {"id": 9, "tasks": ["3.7", "3.9", "3.25", "3.27", "3.33"]},
    {"id": 10, "tasks": ["3.10", "3.11", "3.12", "3.26", "3.29"]},
    {"id": 11, "tasks": ["3.13", "3.28", "3.30", "3.34"]},
    {"id": 12, "tasks": ["3.5", "3.14", "3.15", "3.18", "3.21", "3.22", "3.31", "3.32"]},
    {"id": 13, "tasks": ["3.16", "3.17", "3.19", "3.20"]},
    {"id": 14, "tasks": ["3.23"]},
    {"id": 15, "tasks": ["4.1"]},
    {"id": 16, "tasks": ["4.2"]},
    {"id": 17, "tasks": ["4.3", "4.4"]},
    {"id": 18, "tasks": ["4.5", "4.7"]},
    {"id": 19, "tasks": ["4.6", "4.12"]},
    {"id": 20, "tasks": ["4.8", "4.14", "4.15", "4.18", "4.19", "4.20", "4.21", "4.24"]},
    {"id": 21, "tasks": ["4.9", "4.28"]},
    {"id": 22, "tasks": ["4.10", "4.11", "4.22", "4.23", "4.25", "4.26", "4.30"]},
    {"id": 23, "tasks": ["4.13", "4.16", "4.17", "4.27", "4.29", "4.31"]},
    {"id": 24, "tasks": ["6.1"]},
    {"id": 25, "tasks": ["6.2"]},
    {"id": 26, "tasks": ["6.3", "6.7"]},
    {"id": 27, "tasks": ["6.4"]},
    {"id": 28, "tasks": ["6.5", "6.6", "6.9"]},
    {"id": 29, "tasks": ["6.8", "6.10"]},
    {"id": 30, "tasks": ["8.1"]},
    {"id": 31, "tasks": ["8.2"]},
    {"id": 32, "tasks": ["8.3", "8.9"]},
    {"id": 33, "tasks": ["8.4"]},
    {"id": 34, "tasks": ["8.5"]},
    {"id": 35, "tasks": ["8.6"]},
    {"id": 36, "tasks": ["8.7", "8.8"]},
    {"id": 37, "tasks": ["8.10", "8.11", "8.12", "8.13"]},
    {"id": 38, "tasks": ["10.1"]},
    {"id": 39, "tasks": ["10.2"]},
    {"id": 40, "tasks": ["10.3", "10.7", "10.8"]},
    {"id": 41, "tasks": ["10.4"]},
    {"id": 42, "tasks": ["10.5", "10.6", "10.10"]},
    {"id": 43, "tasks": ["10.9", "10.11"]}
  ]
}
```
