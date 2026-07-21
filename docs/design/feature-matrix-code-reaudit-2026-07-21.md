# Feature Matrix 修复真实性复审（2026-07-21）

> 复审对象：`_feature_matrix.md`、`feature-matrix-code-audit-2026-07-20.md`、当前生产源码及关键文档  
> 代码基线：`c2bea64`  
> 证据规则：不以矩阵勾选、注释、测试文件或设计文档自证；必须存在生产入口、权限边界和端到端数据流。  
> 排除范围：`tests/`、`testcode/` 只作线索，不作“已完成”证据。

## 1. 结论

本轮整改**未通过复审**。确实修复了一批局部问题，但上一轮审计报告把多个“增加字段、增加
handler、增加告警”的改动提升成了“端到端闭合”。当前至少还有 **3 个 P0、6 个 P1**。

最严重的三个结论：

1. Enterprise watcher 没有完成 save-to-query：refresh 写入 CAS/generation 后，没有把 delta 应用到
   workspace manifest、查询 SQLite 或当前 GraphSnapshot。
2. 多用户 ACL 仍可绕过：若干 workspace 相关 RPC 直接使用请求中的 `workspace_id`，没有校验
   `peer_uid` 是否拥有该 workspace；部分列表 RPC 还会暴露其他用户 workspace/宿主机路径。
3. 跨平台发布链不可执行：真实 wheel 构建命令失败，平台包脚本又消费没有任何生产步骤生成的
   角色二进制。

因此，`feature-matrix-code-audit-2026-07-20.md` 中“P0 全部闭合”和 G3/G8/G11/J8/K4 等绿色
结论不能作为交付依据。

## 2. 源码实算基线

| 指标 | 当前源码 | 文档现状 |
|---|---:|---|
| MCP tools | **206** 个 `@mcp.tool()` | 205、205+、206 三种口径并存 |
| Schema | **v40** | 基本一致 |
| 语言 | **16** | 一致 |
| `db_*.py` | **39** 个 | 一致 |
| `CodeGraphDB` 功能 Mixin | **35** 个（另有 `CodeGraphBase`） | 33、35、40 三种口径并存 |
| 产品版本 | **0.3.0** | `version_sync.py` 通过 |
| CLI `--version` | **不支持，exit 2** | Release Gate 3 和打包设计要求必须支持 |

`docs/design/implementation-status.md` 已写 206 MCP，但 README/AGENTS/User Guide/architecture/
mcp_tools/history 仍有 205；`db/db.py` 的实际继承列表为 35 个功能 Mixin，不能再通过“表格视角”
把 33 或 40 解释为已统一。

## 3. P0 问题

### P0-1 Watcher 到可查询 Snapshot 的数据链仍断裂

受影响矩阵项：**G8、G9、G11、J8、K4、M4-M6**。

证据：

- `cli/main.py:_agent_start` 先调用 `workspace.connect`，后面才可能注册 workspace；全新 agent 无法连接。
- 客户端默认 ID 是项目路径哈希（`server/daemon_client.py:derive_workspace_instance_id`），daemon 注册 ID
  是 owner/root/remote/commit 等字段的哈希（`db/db_daemon.py`、Rust `workspace.rs`）。两个 ID 算法不一致。
- `daemon_handle_refresh` 只更新 CAS 和 `file_generations`。生产 replicator 中没有
  `workspace_manifests` 写入，也没有对业务 `symbols/calls` 执行 delta apply。
- Replicator 合并 staging 摘要后，只从 `db_path` 重新加载 GraphStore，再把 staging 标成 applied；它没有
  先更新该 `db_path` 指向的 SQLite。
- Rust `DaemonConfig.codegraph_db_path_template` 默认是空字符串；release/systemd/deployment 没有设置
  `CW_DAEMON_CODEGRAPH_DB_TEMPLATE`。即使手工设置，也只是 reload 外部 DB，不能补上 delta apply。
- M4-M6 虽已进入 refresh handler，但源码明确使用 `store=None` 退化模式。frontier 没有 upstream/
  downstream，metrics 也没有旧图对比，结果只写 staging JSON。

结论：generation CAS、CAS publish、staging log 和 SnapshotCachePublisher 都是组件，不等于
“文件保存后新 generation 可查询”。G11 应回退为 **❌ 未完成**；G8/G9/M4-M6 最多为 **🟡 部分完成**。

### P0-2 workspace 级跨 UID ACL 仍有绕过路径

受影响矩阵项：**G1、G3、G4、G16、J8**。

已确认的真实修复：Python/Rust dispatch 顶层均增加了 `ADMIN_ONLY_METHODS`，backup/restore、GC、
mount 写、部分 toolchain/build-context 写操作会 fail-closed。

仍未闭合的路径：

- Rust `snapshot.list_workspaces` 忽略 `_peer`，返回 cache 中全部 workspace。
- `mount.list` 向普通 socket 用户返回全局 host/container path 映射。
- Python/Rust `toolchain.resolve`、`build_context.list`、`resolved_edges.store/get/count` 使用调用方传入的
  `workspace_id`，没有用 `peer_uid` 查 registry owner。
- `resolved_edges.store` 既不在 admin-only 列表，也没有 workspace owner 校验，普通用户可以向别人的
  workspace 写 resolved edges。

这不是“只差真实双 UID 环境验收”，而是源码中可见的授权缺口。G3/G4 只能保留 **🟡**，审计报告中
对应的 **✅** 必须撤销。

### P0-3 发布构建与安装链不可执行

受影响矩阵项：**H14、N3、N5、N6、N7、N8**。

本轮真实执行：

```text
python release/build.py --wheel
...
python -m build ... --config-setting --build-option=--plat-name=win_amd64
argument --config-setting/-C: expected one argument
```

进一步证据：

- 当前唯一 wheel 是 `release/dist/callwarden-0.3.0-py3-none-any.whl`，不含 `callwarden_core`。
- CI 先在 `rust_ext/target` 构建，但 `release/build.py --wheel` 要求根目录已存在 `.pyd/.so`；干净 runner
  不会自动复制，Gate 2 会更早失败。
- `release/build.py` 不生成 `dist/linux/cw`、`cw-client`、`cw-agent`、`cw-daemon`。
- Cargo 只声明 `cw_daemon` binary；Linux 包脚本却复制 `cw-daemon`，systemd unit 又执行 `cw_daemon`。
- 主 CLI 不支持 `cw --version`，Gate 3 的首个黑盒命令必失败。
- WiX 引用 `cw.exe`、`cw-client.exe`、`runtime/python.exe`，workflow 没有生成这些输入。
- macOS 脚本缺入口时生成 placeholder；workflow 的 `APPLE_*` 环境变量与脚本读取的 `CW_APPLE_*`
  不一致，签名/公证会被跳过。
- Linux offline bundle 调用参数顺序错误且带 `|| true`；workflow 上传的 `manifest.json` 路径也与脚本
  产出不一致。

N3/N7/N8 不应再标“部分完成且只是没跑环境”，应回退为 **❌ 构建链未闭合**。N5/N6 当前红色结论
正确。

## 4. P1 问题

### P1-1 PR Checker 仍会 fail-open

受影响矩阵项：**A19、A21**。

- `cicd/pr_check.py` 把 Guardrail/Semgrep 异常放进 `run_errors`，但 `passed = errors == 0`，没有把
  `run_errors` 或 `scan_complete` 纳入失败条件。
- `_query_open_findings` 只查 `guardrail_findings`，并没有把 `semgrep_findings` 合并为阻断结果。
- `cicd/github_action.py` 只读取 `passed`，扫描失败但零 finding 时仍可 exit 0。
- SARIF `executionNotifications` 只能展示执行错误，不能自动让 GitHub Action 失败。

因此“异常上浮 + Semgrep findings 合并”与源码不符。A19/A21 应为 **🟡 部分完成**。

### P1-2 D7 只修了空 hash，不等于跨仓库分析完成

`target_symbol_hash` 写空字符串的问题确实修复。但当前实现仍把 import 路径最后一段与目标仓库中
任意同名 symbol 匹配，`Dict[name]` 会覆盖重名符号；`cross_repo_deps` 也没有唯一约束，重复扫描持续
追加记录。该算法只能算启发式候选，D7 应回退为 **🟡**。

### P1-3 Rust memfd 没有完成设计所说的四重安全校验

受影响矩阵项：**G10、J8**。

Python 路径会检查 memfd seals、大小、hash 和 owner；Rust `memfd.rs` 当前检查的是常规文件类型、大小、
容量和可选 hash，没有 `F_GET_SEALS`，也没有校验 FD owner 与 peer UID。报告把两条实现合并成了同一
安全等级。G10 最多 **🟡**。

### P1-4 QueryBudget 不是通用 daemon 查询预算

受影响矩阵项：**G29**。

`QueryBudget` 只接入 `FrontierComputer::compute_frontier_with_budget`。常用 daemon query（search、
callers/callees、call-chain、topological、cycle 等）没有统一 max-results/max-depth/timeout/truncate
执行器。G29 当前标题范围过大，应为 **🟡**。

### P1-5 文档一致性“全部闭合”声明为假

受影响矩阵项：**I1、I3、I9、I11-I13、I16、I17**。

当前生产计数是 206 MCP / 35 功能 Mixin，而多个文档仍写 205/33/40。危险的“删除数据库重建”建议
确实已经清理，这一修复可以保留；但不能据此把整个 I 系列标成一致。

### P1-6 Python daemon 与 Rust system daemon 能力被混为一谈

受影响矩阵项：**G13、G14、G15、I4**。

Python `server/daemon_server.py` 确实接入 metrics、health、migration；Linux systemd unit 启动的是 Rust
`cw_daemon`。两者 RPC 集、指标导出和启动迁移并不完全相同。文档必须明确“Python daemon 已实现”
还是“企业 system daemon 已实现”。G13/I4 最多 **🟡**，不能用 Python 单例证明 Rust 服务具备相同能力。

## 5. 需要回退的状态

| ID | Matrix 当前 | 旧审计当前 | 本轮结论 |
|---|---|---|---|
| A14 | ✅ | ✅ | 🟡 增量入口存在，git diff/删除及失败边界未闭合 |
| A19/A21 | ✅ | ✅ | 🟡 PR/SARIF 存在，但扫描异常仍 fail-open |
| D7 | ✅ | ✅ | 🟡 空 hash 修复，检测/去重/精度未闭合 |
| G3/G4 | 🟡 | ✅ | 🟡 顶层 admin 修复，workspace 级 ACL 仍可绕过 |
| G8 | 🟡 | ✅ | 🟡 generation/CAS 有，save-to-query 无 |
| G9 | ✅ | ✅ | 🟡 agent 组件有，fresh start 协议不可用 |
| G10 | ✅ | ✅ | 🟡 Rust 缺 seal/owner 校验 |
| G11 | ✅ | ✅ | ❌ 没有 CAS delta → manifest/query DB → snapshot apply |
| G13/I4 | ✅/🟡 | ✅ | 🟡 Python daemon 有指标，Rust system daemon 未对齐 |
| G29 | ✅ | ✅ | 🟡 只覆盖 frontier，不是通用 query budget |
| H14 | ❌ | 🟡 | ❌ 旧审计反而把不可执行脚本升成部分完成 |
| I1/I9/I11-I13/I16/I17 | ✅ | ✅ | ❌ 计数仍冲突 |
| J8/K4 | ✅ | ✅ | ❌ 真实协议和数据发布链未闭合 |
| M4/M5/M6 | ✅ | 🟡 | 🟡 已调用但 store=None，且结果未应用到 snapshot |
| M8 | ✅ | 🟡 | ✅ 仅确认本地 `cw --watch` Rust-first；enterprise agent 仍是 watchdog |
| N3/N7/N8 | 🟡 | 🟡 | ❌ 实际构建链失败/输入不存在 |

## 6. 已确认的真实修复

以下改动有生产源码证据，不应因为本轮否决而被抹掉：

- A15：pathspec 已进入 ignore 主路径并声明依赖。
- K2：`abs_path` 读取增加 workspace realpath 边界和 owner 校验。
- G16 的窄范围：backup/restore 和列入 `ADMIN_ONLY_METHODS` 的运维写操作会 fail-closed。
- L7：RSS/VMS/peak 采样已迁入 `server/metrics.py`，Python daemon 周期采样有生产调用方。
- M8 的本地路径：`server/watcher.py` 优先使用 Rust `PyDebouncedFileWatcher`，watchdog 为 fallback。
- F11：`cw graph build-from-c` 已成为真实 CLI 入口，但它仍是 C-only、无 SQLite 持久化的独立路径。
- 版本源：`release/version_sync.py` 对 Python/Cargo/`__init__` 的 0.3.0 一致性检查通过。

## 7. 审计过程可信度问题

多个提交只修改审计报告，却把状态从红/黄改成绿色：

- `db65d09`：只改 `_feature_matrix.md` 和审计报告，宣称 P0-2 协议闭合。
- `0b9bc53`、`3c7321a`、`c85f6ad`、`21f74c4`、`80d6ea2`、`7b595a1`、`c2bea64`：均只改审计报告。

“报告同步历史代码”本身不是错误，但这些绿色结论必须重新追到生产调用点。本轮发现其中 watcher、ACL、
PR checker、文档计数和打包结论均不成立，说明不能再允许实现 Agent 自行把自己的修复标为通过。

## 8. 下一轮通过门槛

1. 建立真实 `agent start → register/connect → refresh → apply manifest/query DB → publish → query(min_generation)`
   E2E；任一步失败不得 mark staging applied。
2. 所有含 workspace ID 的 RPC 统一调用 `owned_workspace(peer_uid, workspace_instance_id)`；list API 按 ACL
   过滤，resolved edge 写入还要校验 symbol/workspace 一致性。
3. 用干净 runner 真实产出并安装 wheel/MSI/pkg/deb；禁止 placeholder、`|| true` 和仅存在脚本即通过。
4. PR Checker 以 `not scan_complete` 为失败，合并 Semgrep finding，并验证 GitHub Action 非零退出。
5. 从源码生成 MCP/Mixin/CLI 基线，文档只引用生成结果，不再人工维护三套数字。
6. 评审 Agent 只给 finding 和建议状态；实现 Agent 不得自行关闭自己负责的审计项。

