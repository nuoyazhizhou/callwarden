# Feature Matrix 整改真实性复审（2026-07-22）

## 1. 结论

复审对象：批次 27-34，重点核验上一轮报告中的 3 个 P0、6 个 P1，以及
`_feature_matrix.md` 对相关条目的最新状态。

**“9 项 P0/P1 已全部处理完成、无剩余未处理项”不成立。** 当前可以确认：

- 1 项核心修复成立：P1-3 Rust memfd 已补 owner UID 和 seals 校验；
- 2 项只完成了文档纠偏：P1-4 QueryBudget、P1-6 Python/Rust daemon 能力区分；
- 3 项部分修复：P1-1 PR Checker、P1-2 D7、P1-5 基线同步；
- 3 项 P0 仍未闭合：多用户 ACL、watcher save-to-query、跨平台发布链。

当前不应把 G1/G3/G4、G8/G11、J8/K4 标为完成，也不能把 N6/N7 的问题描述成
“只差干净 runner 验证”。这里存在源码静态可证的生产故障。

## 2. 整改逐项判定

| 原问题 | 当前判定 | 说明 |
|---|---|---|
| P0-1 watcher save-to-query | ❌ 未闭合 | Python 路径存在非原子提交、事实不完整和跨 workspace snapshot 污染；真实 Linux Rust daemon 未做 CAS -> CodeGraph merge |
| P0-2 workspace 跨 UID ACL | ❌ 未闭合 | Python 入口补了 ACL，但 Rust system daemon 的 5 个 workspace-id handler 仍忽略 peer |
| P0-3 发布构建与安装链 | ❌ 未闭合 | Windows MSI gate 固定失败；Linux/macOS 包复制临时 venv launcher，安装后不可执行；Linux 升级脚本调用不存在的 daemon 子命令 |
| P1-1 PR Checker fail-open | 🟡 部分修复 | `passed` 已纳入 `scan_complete`，Semgrep findings 已合并；git diff 失败、Semgrep 返回 `success=false`、finding SQL 失败仍会 fail-open |
| P1-2 D7 跨仓库分析 | 🟡 部分修复 | UNIQUE、target hash、重名候选已修；影响方向、跨 workspace 去重和歧义匹配仍错 |
| P1-3 Rust memfd | ✅ 核心缺口已修 | owner UID + seals 已进入生产调用；仍有普通文件跳过 seals、长度/hash 非强制的 P2 收紧项 |
| P1-4 QueryBudget | 📄 仅文档纠偏 | 批次 33 没有功能代码改动，文档也明确写“未修复代码缺陷” |
| P1-5 基线一致性 | 🟡 部分修复 | 206/35/40/v41 源码计数正确；“76 文档零不一致”被扫描盲区推翻 |
| P1-6 daemon 能力区分 | 📄 仅文档纠偏 | Python/Rust 能力差异写清了，但 Rust metrics/migration/完整 health 等并未实现 |

## 3. 阻断问题

### P0-1 Rust system daemon 仍可跨 UID 访问 workspace 数据

Linux systemd unit 启动的是 Rust `cw-daemon`，不是 Python daemon。当前 Rust handler：

- `handle_toolchain_resolve`：`snapshot_state.rs:960-963`
- `handle_build_context_list`：`snapshot_state.rs:1023-1026`
- `handle_resolved_edges_store`：`snapshot_state.rs:1074-1077`
- `handle_resolved_edges_get`：`snapshot_state.rs:1123-1126`
- `handle_resolved_edges_count`：`snapshot_state.rs:1146-1149`

均把凭据命名为 `_peer` 并忽略，直接使用调用方提供的 `workspace_id`。批次 29 只在
Python handler 接入 `_owned_workspace_by_id`，Rust 侧仅修了 `snapshot.list_workspaces`。

因此 G1/G3/G4 的“P0-2 已闭合”和 J8 的“全部修复”是错误状态。

### P0-2 真实 Linux daemon 没有 CAS -> CodeGraph -> Snapshot 数据链

`release/linux/deb/systemd/callwarden-daemon.service:33-35` 启动 Rust binary。Rust 默认
`codegraph_db_path_template` 为空（`rust_ext/src/daemon/config.rs:72`），unit 也没有设置
`CW_DAEMON_CODEGRAPH_DB_TEMPLATE`。即使管理员手工配置，Rust
`SnapshotCachePublisher::publish_snapshot` 只调用
`build_and_publish_blocking(db_path, ...)` 重新加载现有 SQLite
（`rust_ext/src/daemon/replicator.rs:740-768`），没有把本次 CAS delta 写入该数据库。

批次 30 新增的 `merge_cas_to_codegraph()` 只在 Python `server/replicator.py` 调用，Rust
daemon 没有等价实现。故真实企业部署的 watcher save 后仍不能保证新 generation 可查询。

G8、G11、J8、K4 必须回退；G11 当前备注一边标绿、一边承认 Rust 路径未接入，本身矛盾。

### P0-3 Python save-to-query 路径仍可能丢事件并泄漏其他 workspace

Python 路径先提交 `latest_committed_generation`
（`server/replicator.py:254-269`），之后才执行 CodeGraph merge 和 manifest upsert
（`:277-390`）。后续失败时同一 seq 重试会在 `:223-226` 被当作 stale 丢弃，事件永久无法恢复。

此外，`merge_cas_to_codegraph()` 单独提交 CodeGraph DB（`db/db_cas_merge.py:366-367`），
再提交另一数据库里的 manifest，跨库不原子。合并器只写 `file_contents`、`symbols`、`calls`，
没有写 `symbol_contents`/版本事实；而 `symbols.symbol_hash` 外键指向
`symbol_contents.content_hash`（`db/schema.py:64`）。它还只删除旧 caller 的出边，不清理指向
被替换 symbol 的入边，并以 `callee_id=0` 保存未解析调用。

更严重的是 Python 默认对所有 workspace 使用同一个用户级 `callwarden.db`
（`server/daemon_config.py:191-204`），而 Rust GraphStore 装载 symbols 的 SQL 没有
`workspace_id` 条件（`rust_ext/src/graph.rs:467-472`）。每个“workspace snapshot”实际会装入
整个数据库的符号；外层 RPC ACL 即使正确，也挡不住 snapshot 内部混入其他 workspace/UID 数据。

### P0-4 三平台安装/发布链是确定性失败，不只是缺 E2E 证据

Windows：`enterprise-release.yml:239-250` 明确因缺少 PyInstaller 的 `cw.exe`、
`cw-client.exe`、`runtime/python.exe` 而 `exit 1`，所以 11 gate release 目前必停在 Gate 4a。

Linux：`release/linux/build_packages.sh:119-136` 在临时 venv 安装 wheel，随后仅复制
该 venv 的 console scripts 到包内（`:169-195`）。这些脚本的 shebang 指向构建机临时
venv，但包中没有该 venv、Python 模块或依赖；control 文件甚至只依赖 libc/libgcc。
安装后的 `cw`/`cw-client`/`cw-agent` 无法启动。

macOS 同样只复制临时 venv 的 launcher（`release/macos/build_pkg.sh:164-190`），没有打包
解释器/site-packages。Gate 2 还声明无效 Rust target `universal2-apple-darwin`
（`.github/workflows/enterprise-release.yml:108`），实际 cargo 命令又没有使用 matrix target
（`:120`）。

Linux maintainer scripts 调用了 Rust CLI 不存在的命令：

- `schema-check --pre-upgrade`：`daemon.preinst:31`，但 binary 只接受 `--strict`；
- `drain` / `snapshot create`：`daemon.preinst:55,67`；
- `migrate`：`daemon.postinst:93`；
- Rust `Command` 实际只有 Serve、SchemaCheck、HealthCheck（`cw_daemon.rs:104-127`）。

当前 Windows wheel 的单平台基础构建已经修复：本轮实际运行
`python release/build.py --wheel` 成功生成 `cp314-cp314-win_amd64` wheel，并在独立 venv
从 site-packages 加载 `callwarden_core.pyd`。这不能证明 MSI/deb/pkg 或 11 gate 已闭合。

## 4. 其他重要问题

### P1-1 PR Checker 仍有三条 fail-open 路径

已确认的修复：`passed = errors == 0 and scan_complete`，GitHub Action 对 `passed=false`
返回 1，Semgrep findings 已并入汇总。

仍未修复：

1. `IncrementalAnalyzer.get_changed_files()` 在 git diff 非零时返回空列表
   （`cicd/incremental.py:54-58`）；PRChecker 将其解释为“无改动”并通过。
2. `scan_semgrep_incremental()` 用 `{success: false, error: ...}` 表示扫描失败；
   `PRChecker.run_pr_check()` 只捕获异常，不检查返回值（`cicd/pr_check.py:99-109`）。
3. `_query_open_findings()` 对 guardrail/Semgrep 两个 SQL 异常都静默 `pass`
   （`cicd/pr_check.py:232-263`），查询损坏可变成零 finding。

Semgrep 查询还只按 `rel_path` JOIN，没有 active `workspace_id` 条件，可能把另一个 workspace
同路径的 finding 混入本次 PR。

### P1-2 D7 去重与候选改进成立，但影响传播仍有方向错误

批次 32 确实增加 v41 五元组 UNIQUE、`INSERT OR IGNORE`、target hash 和重名候选列表。
但记录语义是“source symbol import target symbol”。`cross_repo_impact()` 却把
`source_symbol_hash = changed_symbol` 的 target workspace 也列为受影响仓库
（`db/db_cross_repo.py:438-453`）；改变调用方不会反向影响被调用库，方向错误。

同轮去重 key 只有 `(source_hash, target_hash)`（`:217-221`），没有 target workspace。
同一 CAS symbol 出现在多个目标仓库时，后续仓库会被误去重。短名候选循环中的
`import_path.endswith(cand_qn.split(".")[-1])` 对按同名筛出的候选恒真，仍会任取第一个重名
symbol，不能称为可靠 FQN 匹配。

### P1-3 memfd 原始问题已修，但“六重校验”不是所有 FD 都强制执行

Rust 生产调用已传 `peer.uid`，memfd 会要求完整 seals，原 P1-3 可以通过。剩余 P2：

- 普通文件 FD 在 `F_GET_SEALS` 返回 EINVAL/ENOTTY 时明确跳过 seals；
- Rust 不校验 `st_size == canonical_len`；
- `expected_sha256/content_hash` 可缺省，此时摘要校验跳过。

Python `validate_memfd_fd()` 则强制 declared length、seals、上限和 hash。矩阵可以写“memfd
核心校验已补齐”，不宜把两条路径描述成完全相同的六重强制协议。

### P1-4/P1-6 是正确的文档降级，不是功能完成

批次 33 只修改 `_feature_matrix.md`、`implementation-status.md` 和文档断言测试。
`implementation-status.md:273-278` 明确写“未修复代码缺陷，仅做能力区分文档化”。

因此，把 G28/G29、G13/G14/G15 维持黄色是正确整改；把这说成 QueryBudget、Rust metrics、
Rust migration 或完整 health 已实现，则是错误解读。

### P1-5 源码计数正确，但基线脚本产生了假阴性

本轮独立实算：206 MCP decorators、35 功能 Mixin + 1 base、40 个 `db_*.py`、16 语言、
Schema v41，均与 `scripts/check_baseline.py --json` 一致。

但 `--check` 的“全部 76 个文档一致”不可信：

- `docs/architecture.md:49,323` 仍写 39 个文件；扫描器只匹配“N 个 db_*.py”，抓不到
  ``db_*.py`（39 个文件）`；
- `_feature_matrix.md:362` 的 I38 仍写 33 Mixin/39 文件；因为该行含 `→`，整行被
  `SKIP_MARKERS` 跳过；
- 扫描器输出使用传入路径总数 76，不是实际扫描文件数；它还整目录/整文件跳过多份文档。

所以源码数字同步大部分完成，但 I1/I17 所称“76 文档零不一致”应撤回。

## 5. 矩阵建议状态

必须回退：

- G1/G3/G4：✅ -> 🟡（Python ACL 已补，Rust system daemon 未补）
- G8/G11：✅ -> ❌（真实 Rust daemon save-to-query 未闭合；Python 路径也不安全）
- J8/K4：✅ -> ❌（纵向协议链未闭合）
- N6/N7：🟡 但备注改为“安装产物确定不可运行”，不是“仅待 runner 验证”
- N8：🟡，明确 11 gate 当前必在 Windows Gate 4a 失败
- I1/I17：✅ -> 🟡（基线值正确，全文档一致性声明错误）

可以保留：

- A19/A21：🟡，但备注更新为当前残余 fail-open 路径；
- D7/I2/I7/I19：🟡；
- G10：✅（原 P1-3 核心缺口已修），备注保留普通 FD/长度/hash 边界；
- G13/G14/G15/G28/G29：🟡，当前文档对 Python/Rust 和预算缺口的区分基本准确；
- N5：❌。

## 6. 验证记录

- `python release/build.py --wheel`：成功；Windows cp314 wheel 含 Rust extension。
- 独立 venv 安装 `--no-deps` wheel：`callwarden_core.pyd` 从 site-packages 成功加载。
- `python scripts/check_baseline.py --json`：206/40/35/36/16/v41/0.3.0。
- `python scripts/check_baseline.py --check`：表面通过，但由上述两个现存冲突证明存在扫描盲区。
- `git status --short`：复审前产品 worktree clean；构建产物未形成 tracked 修改。

本报告不以任务状态、commit message 或测试文件中的断言作为完成证据；所有结论均追踪到
生产入口、systemd 实际启动路径、持久化顺序和公开查询边界。
