# Rust-only Parser 生产切换独立复审报告

> 日期：2026-07-26  
> 独立复审任务：`T-1785022737899-007ca91c`  
> 被审实现任务：`T-1784986236712-736b2331`  
> 被审提交：`5592277..eaceca7`  
> 结论：**拒绝 apply/close，存在 5 个 P0 + 4 个 P1**

## 1. 复审结论

实现任务树显示 52/52、8 个子任务均为 `review`，但这只说明任务状态被推进，
不代表生产切换完成。本轮从提交态、当前工作区、生产调用链、真实构建产物四层
重新取证，确认：

1. 提交态 `eaceca7` 不能编译。
2. ParseFact 契约门禁会在 Rust 测试全部 skipped 时报告 pass。
3. 设计要求的 local ID、parent ID、call ordinal、canonical byte range、signature
   仍未由 Rust parser 产出。
4. Python parser 仍在 `CodeGraphDB` 生产初始化链中，删除 parser 后 local 包启动即崩溃。
5. daemon 的失败保护、retry log、metrics/doctor 只是孤立模块，没有接入 refresh /
   generation / snapshot 发布主链；失败状态仍可能被 committed。
6. 四平台 workflow 尚未推送或运行，包体报告没有任何实测数据。

因此原父任务及 8 个实现子任务必须继续停在 `review`，不得 apply 或 close。

## 2. P0 阻塞项

### P0-1：提交态无法编译，未提交修改掩盖失败

在 detached worktree 对 `eaceca7` 执行：

```text
cargo check --manifest-path rust_ext/Cargo.toml
```

得到 13 个 `E0063`：Python、Rust、Go、Java、TypeScript、JavaScript、Ruby、PHP、
Scala、C#、C++、Kotlin、Swift 的 `LangConfig` 初始化缺少
`import_directives` / `reference_rules`。这两个字段在
`rust_ext/src/multi_lang.rs:216-227` 已成为必填字段，但补齐 13 种语言的修改仍留在
当前脏工作区，未进入 `eaceca7`。

当前工作区还有 24 个 tracked 文件未提交及 `_probe_ast.py`、
`_probe_output.txt`、`callwarden_core.pyd.old_step0` 等调试残留。实现任务不能以
当前脏工作区的测试结果证明提交态可交付。

### P0-2：ParseFact 契约门禁是假绿

`tests/parser_contract/gate_report.py:301-305` 只检查 `failed == 0`，不把
`skipped > 0` 或 Rust 扩展不可用视为失败。本机根目录 `callwarden_core.pyd`
加载失败时，报告仍返回：

```text
overall=pass, rust_extension_available=false
kind/signature/visibility: 0 passed, 92 skipped
identity/range:            0 passed, 240 skipped
encoding/error:            0 passed, 70 skipped
```

即使使用本轮隔离构建的可加载 wheel，`gate_report` 的 pytest 子进程仍把 cwd
切回仓库根目录，被损坏的根级 pyd 抢占，最终出现
`rust_extension_available=true` 但上述 402 项仍全部 skipped 的矛盾报告。

门禁内容本身也没有闭合：

- `tests/parser_contract/test_golden_fixtures.py:1-5` 明确只检查 JSON 结构，不比较
  当前 parser 输出。
- `tests/parser_contract/test_identity_range.py:575-665` 把 ABI 字段缺失写成
  “字段必须继续不存在”的通过测试。
- `tests/test_rust_python_alignment.py:673-747` 只比较双方均非空的 signature，
  同时断言 Rust signature 全空，所以比较项为 0。

实际 Rust 输出结构 `rust_ext/src/lib.rs:84-110` 没有 `local_id`、
`lexical_parent_local_id`、`caller_local_id`、`call_ordinal`、byte range；
`rust_ext/src/daemon/replicator.rs:626-653` 明确把 parent/range 写成 `None/0`。

### P0-3：删除 Python grammar 后 local wheel / frozen bundle 不可运行

生产导入链仍是：

```text
callwarden/__init__.py:15
  -> callwarden.db
  -> db/db_base.py:14
  -> callwarden.parsers
  -> parsers/base.py:19
  -> tree_sitter
```

而且 `db/db_base.py:2132` 每次初始化仍实例化 Python `RustParser`。
`tests/test_rust_only_parser_boundary.py:270-284` 不是消除此调用，而是整体豁免
`db_base.py`。

当前 `pyproject.toml` 默认依赖和脏工作区
`release/pyinstaller/requirements-runtime-local.txt` 已移除 tree-sitter，
`release/pyinstaller/callwarden.spec:217-265` 又排除了
`callwarden.parsers` 与所有 grammar，故依赖图无法成立。

本轮在隔离目录完成 Windows PyInstaller 构建后，执行：

```text
cw.exe --version
```

立即失败：

```text
ModuleNotFoundError: No module named 'callwarden.parsers'
```

此外，提交态 `HEAD` 的 `requirements-runtime-local.txt` 仍包含 tree-sitter
核心和 16 个 grammar；所谓删除依赖只存在于未提交工作区。

### P0-4：rust-strict 失败保护与恢复没有接入 daemon 主链

`SnapshotGenerationGuard`、`ParseRetryLog`、`ParserMetrics`、`ParserDoctor`
仅在各自模块、单元测试和 `daemon/mod.rs` 导出中出现；`dispatch.rs`、
`workspace.rs`、`replicator.rs`、`server.rs` 没有生产调用。

当前真实行为相反：

- `rust_ext/src/daemon/replicator.rs:318-343` 不检查 `cas_state`，即使
  `parse_failed` / `unsupported_language` / `cas_lookup_failed` 也返回
  `status="committed"`。
- `rust_ext/src/daemon/workspace.rs:1184-1190` 因此继续追加 staging entry。
- `rust_ext/src/daemon/workspace.rs:1556-1563` 在 `merge_summary=None` 时把
  `merge_ok` 视为 true，继续 generation committed 和 replicate。
- `rust_ext/src/multi_lang.rs:1251-1265` 也承认当前 ParseResult 不含 syntax /
  unsupported 计数，只能返回 `Ok/Failed`。

本轮用畸形 Python/Rust canonical bytes 实测，两个 parser 均返回
`error=None`；其中 Python 甚至提取出一个 symbol。现有
`test_daemon_handle_refresh_parse_failure_returns_parse_failed`
在 `rust_ext/src/daemon/replicator.rs:1879-1901` 实际只传空文件并期望
`ready_published`，并未测试 parse failure。

### P0-5：发布 inspector 与真实 artifact 均未闭合

隔离 Windows bundle 构建得到 133.91 MiB，并重复收集两份 Rust 扩展：

```text
_internal/callwarden_core.cp314-win_amd64.pyd                  36.1 MB
_internal/callwarden_core/callwarden_core.cp314-win_amd64.pyd  32.8 MB
```

但 `release/inspect_pyinstaller_bundle.py:152-155,275-289` 只识别精确文件名
`callwarden_core.pyd/.so`，不识别 ABI 后缀，反而报告“Rust 扩展缺失”。
`_verify_rust_parse` 在 `:355-385` 还用模块名 `callwarden_core_verify`
加载 PyO3 扩展，与 `PyInit_callwarden_core` 不匹配。

因此 inspector 既不能正确识别 Rust 扩展，也不能发现重复收集，且不执行
`cw --version/--help/server --check-imports` 的 executable smoke。

## 3. P1 问题

### P1-1：16 语言“对齐通过”仍包含系统性语义缺口

`rust_ext/src/multi_lang.rs:865-906` 将所有 signature 写空，visibility 除 PHP
外默认 `public`。现有测试通过白名单和“双方有值才比较”掩盖差异，不能作为逐语言
放行证据。HCL/Elixir 的样例测试在当前脏工作区可通过，但这不补齐通用 ParseFact ABI。

### P1-2：生产迁移还有错误 fallback 和未实现模式

`server/replicator.py:539-545` 的 fallback 调用
`parse_file_lang(abs_path, "")`，缺少必需的 `language` 参数，路径必然异常。
`db/rust_parser_facade.py:73-190` 的 `shadow` / `python-reference` 只有环境判断，
文档也承认实际 reference adapter 不在本类中；仓库未找到生产 adapter 接线。

### P1-3：P2-H 测试不是声明的真实企业 E2E

四个平台 workflow 位于本地 `eaceca7`，但远端仍是：

```text
origin/master = 9409ede
latest tag     = v0.3.2
```

因此新增 workflow 未在 GitHub 运行，也没有四平台报告可审。

`tests/test_p2h_enterprise_real_workspaces.py` 还有以下证据缺口：

- “10 UID”只是循环修改 `HOME/USERPROFILE`，没有真实 UID 或 daemon ACL。
- `:305-307` 使用无效的 `--refresh-all <workspace>` 调用，失败后 `:311-314`
  直接 skip。
- 重复 parse <5% 在无法从输出判断时 `pass`。
- dirty overlay 测试只检查 refresh exit 0，不查询 Global CAS。
- Docker、SMB、VS Code、容器矩阵缺失时均 skip。

本轮相关 pytest 为 52 passed、16 skipped，不能替代真实环境验收。

### P1-4：任务状态与审计证据不可信

P0-A 的 3 个步骤没有 result，P0-C/父任务状态由实现会话手工 SQL 推进；
8 个实现子任务中只有 P0-C 有 change audit，其余为 0。
自动执行 `completion-review T-1784986236712-736b2331` 仍返回
`pass, 0 findings`，说明该自动门禁没有读取本报告中的真实生产证据。

`docs/design/rust-only-parser-size-report.md` 的 before/after/gate 四平台表全部
仍为“待填充”，但任务步骤 `publish_before_after_size_report` 已被标记 done。

## 4. 验证记录

| 验证 | 结果 | 解释 |
|---|---|---|
| detached `eaceca7` cargo check | **FAIL**，13 个 E0063 | 提交态不可编译 |
| 当前脏工作区 cargo check | PASS，25 warnings | 未提交补丁掩盖失败 |
| parser gate（根级损坏 pyd） | **假 PASS**，402 skipped | fail-open |
| 隔离 wheel alignment/identity/encoding | PASS | 测试契约允许 ABI 缺失 |
| HCL/Elixir 相关 pytest | 47 passed | 仅样例级 |
| daemon Rust tests | 406 passed | helper/unit tests |
| P1-F Rust integration | 15 passed | 手工组合孤立模块，非 daemon E2E |
| P1-F Python tests | 38 passed | 多处只验证返回值或注释 |
| malformed canonical bytes | **error=None** | syntax failure 未识别 |
| Windows PyInstaller build | 构建 PASS | 只证明收集完成 |
| frozen `cw.exe --version` | **FAIL** | 缺 `callwarden.parsers` |
| bundle inspector | **FAIL/误判** | ABI 文件名不识别，Rust pyd 重复 |
| 包装/P2-H pytest | 52 passed, 16 skipped | 真实环境未验收 |
| remote/tag evidence | **无新提交/无新 tag** | 新 workflow 未运行 |

## 5. 重新送审门槛

1. 把所有生产修改提交后，在 detached clean HEAD 上通过 `cargo check` 和完整测试。
2. 契约 gate 必须在 Rust 扩展不可用、任一 suite skipped、0 个比较项时 fail closed。
3. golden fixture 必须与真实 Rust 输出比较；补齐 ID、parent、ordinal、byte range、
   signature、visibility、syntax/unsupported diagnostics。
4. 移除 `CodeGraphDB` 初始化对 `callwarden.parsers` / `tree_sitter` 的生产依赖，
   包括 ModuleResolver 所需的 Rust mod declarations。
5. 把 generation guard、retry log、metrics/doctor 接入真实 daemon refresh /
   commit / snapshot / restart 链，并新增失败 generation 不覆盖的生产 E2E。
6. 修复 inspector 的 ABI 后缀识别和 PyO3 加载名，消除重复 Rust pyd，并把
   executable smoke 纳入 fail-closed 门禁。
7. 在 GitHub 真正运行 Windows amd64、macOS arm64、Linux x86_64/aarch64；
   Linux 另跑真实双 UID、容器挂载、SMB/VS Code、kill -9 与 CAS 污染检查。
8. 用同 runner 的真实 before/after artifact 填完体积报告和 SBOM/upgrade evidence。

在以上门槛全部满足前，独立 Reviewer 的决定保持：**reject**。
