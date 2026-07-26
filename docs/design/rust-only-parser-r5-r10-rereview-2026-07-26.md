# Rust-only Parser R5-R10 整改独立复审

> 日期：2026-07-26
> 复审任务：`T-1785045919090-e9590bdd`
> 被审提交：`a67e972`
> 结论：**拒绝推送、打 tag、apply/close；仍有 4 个 P0 + 3 个 P1**

## 1. 结论

本轮在 detached clean worktree 中重新构建 Rust wheel、运行契约 gate、执行完整
daemon 测试、构建 Windows PyInstaller 产物并运行真实 inspector。不能接受
“R5-R10 整改完成”的总体声明。

已经确认的修复：

1. R5 冻结入口已修复，`cw.exe --version`、`--help`、
   `server --check-imports` 均返回 0。
2. R6 首次 partial parse 已能返回 `partial_published` 并阻止当次 snapshot 替换。
3. R7 `local_id` 已改为从 1 开始。
4. R10 inspector 的真实模块名、ABI 后缀识别和重复扩展 fail-closed 已修复；
   仅保留一份扩展时，`--verify-rust-parse` 返回 0。

但 R6-R9 的生产语义仍未闭合，R10 spec 仍生成双份 Rust 扩展，R11 没有真实
四平台证据。

## 2. P0 阻塞项

### P0-1：partial 事实以普通 ready CAS 发布，第二次 refresh 绕过保护

`rust_ext/src/daemon/replicator.rs:536-546` 在 parse 前先查询任意 `state='ready'`
的 CAS，并把命中统一返回为 `ready_cache_hit`。同文件 `:584-653` 又把 partial
结果用普通 `CasStore::publish()` 发布，只在本次响应中临时返回
`partial_published`。

CAS schema 和 `lookup()` 没保存 parse status/diagnostics：

- `rust_ext/src/daemon/cas.rs:29-41` 只有通用 `state='ready'`；
- `rust_ext/src/daemon/cas.rs:325-331` 只按 ready 查询；
- `rust_ext/src/daemon/snapshot_guard.rs:79` 把所有 `ready_cache_hit` 当成功。

独立 integration probe 已复现：

```text
第一次畸形 Rust refresh: partial_published
第二次相同内容 refresh: ready_cache_hit
test result: 1 passed
```

因此第一次被 guard 阻止的 partial 图谱，第二次会绕过 guard 并可能替换最后一个
好 snapshot。需要把 parse status/diagnostics 纳入 CAS 元数据和 cache-hit 响应，
或禁止 partial 进入可被 ready lookup 命中的命名空间。

### P0-2：ParseFact ABI 仍违背企业设计的 NULL 语义

企业设计 `docs/design/enterprise-phase1-phase3-detail.md:1074-1093` 明确要求：

- `local_id` 从 1 开始；
- `lexical_parent_local_id: Option<u32>`，顶层为 NULL；
- `caller_local_id: Option<u32>`，顶层裸调用为 NULL。

当前 `rust_ext/src/lib.rs:106-127` 仍把 parent/caller 定义成 `u32`，并用 0 同时
表示 synthetic module、顶层或未解析 caller。隔离 wheel 的真实输出为：

```json
{
  "local_id": 1,
  "lexical_parent_local_id": 0,
  "signature": ""
}
```

R7 只修复了 1-based `local_id`，没有实现设计要求的 Option/NULL ABI，也无法在
ParseFact 内区分顶层裸调用与未解析 caller。

### P0-3：signature/visibility gate 仍是假绿

`rust_ext/src/multi_lang.rs:914` 仍为所有通用语言写入
`signature: String::new()`；`:928-940` 说明除 PHP 外 visibility 仍默认 public。

`tests/test_rust_python_alignment.py:790-795` 对 fixture 中带 signature known gap 的
语言直接 `return`，不比较实际差异，也不检查“已知缺口已经多余”。16 个 golden
fixture 均可借该缺口放行。visibility 测试同样用 `KNOWN_VISIBILITY_DIFFS` 接受
报告中明确标注为“Phase 2.7 待修复”的系统性差异。

clean wheel 上 gate 仍报告：

```text
overall_status=pass
775 passed / 0 skipped
```

但同一 wheel 的正常 Rust 函数输出 `signature=""`，gate 报告也公开列出 Python、
Go、JavaScript、Swift 等 visibility 未修复差异。统计层 fail closed 不等于字段
契约完成；当前仍违反“Rust missing 不得靠白名单放行”的切换要求。

### P0-4：完整 daemon 测试集无法编译

按项目规则执行：

```text
cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib --no-default-features
```

clean `a67e972` 在 R9 新增测试处产生 5 个 `E0432/E0433`：

- `workspace.rs:3012` 错误引用 `super::parse_retry_log`；
- `:3026-3052` 同样错误引用 `super::cas`、`super::staging_log`、
  `super::parser_metrics`、`super::replicator`。

因此实现方给出的 `cargo check` 只证明非测试代码可编译，未编译
`#[cfg(test)]` 路径；新增 R9 测试实际从未通过。

## 3. P1 问题

### P1-1：R9 只“重试 CAS”，没有恢复到新 generation 可查询

daemon 启动的 `recover_all_workspaces()` 位于
`rust_ext/src/bin/cw_daemon.rs:185-189,854-930`，只读取和恢复 `staging.log`，
不读取 `parse_retry.log`。

显式 RPC `workspace.recover` 的新逻辑位于
`rust_ext/src/daemon/workspace.rs:1955-2012`，但它只调用
`_daemon_parse_and_publish()`，CAS publish 成功后立即 `mark_applied`。该路径没有：

- 重做持久化 generation compare-and-swap；
- 追加 staging entry；
- merge workspace projection；
- 发布 snapshot；
- 把恢复后的 generation 标记 committed。

所以 daemon 崩溃后不会自动重放 parse retry；即使人工调用 RPC，条目也可能已被
标为 applied，却仍未进入可查询 snapshot。`ParserDoctor` 也仍只有模块内测试调用，
`ParserMetrics` latency 仍固定为 0。

### P1-2：R10 inspector 修好了，但 spec 仍真实生成两份扩展

隔离 wheel + clean PyInstaller 构建成功，冻结三入口也能启动。但产物仍包含：

```text
_internal/callwarden_core.cp314-win_amd64.pyd                   36,172,800
_internal/callwarden_core/callwarden_core.cp314-win_amd64.pyd   36,172,800
```

bundle 总计 137.21 MiB，其中两份 Rust 扩展共 72,345,600 bytes，占 50.28%。
原因是从 hiddenimports 删除 `callwarden_core` 不能阻止 PyInstaller 沿生产代码的
静态 import 自动收集 wheel package；同时 spec 的显式 `binaries` 又收集根级副本。

好的部分是 inspector 已正确 fail closed：

```text
普通 inspector exit 2
--verify-rust-parse exit 2
临时只保留一份扩展后 --verify-rust-parse exit 0
```

所以 R10-a/R10-b 完成，R10-c 未完成。35 个 inspector 单测没有构建真实 spec
产物，因而漏掉了该回归。

### P1-3：R11 真实发布证据仍不存在

实时远端检查：

```text
origin/master = 9409ede
latest tag    = v0.3.2
```

`docs/design/rust-only-parser-size-report.md:86-136` 的四平台 before/after 表仍全部
是“待填充”。新增依赖说明准确描述了为什么数据不存在，但说明依赖不等于完成 R11。
在当前 P0/P1 修复前也不应推送或打 release tag。

## 4. 验证记录

| 验证 | 结果 |
|---|---|
| detached clean HEAD | PASS，`a67e972` |
| `git show --check a67e972` | FAIL，复审报告 3 行 trailing whitespace |
| `cargo check` | PASS，22 warnings |
| 完整 daemon lib tests | **FAIL，5 个 Rust 编译错误** |
| isolated Rust wheel | PASS |
| parser gate | 表面 PASS，775 passed，但 signature/visibility 假绿 |
| partial CAS 二次命中 probe | **复现 `partial_published -> ready_cache_hit`** |
| inspector 单测 | PASS，35 passed |
| Windows PyInstaller build | PASS |
| frozen 三入口 smoke | PASS，3/3 |
| bundle core 数量 | **FAIL，2 份，共 72,345,600 bytes** |
| inspector duplicate gate | PASS，正确 exit 2 |
| inspector single-core real load | PASS，exit 0 |
| 四平台 CI / tag / size report | 未执行、未填充 |

## 5. Reviewer 决定

1. 不推送 `a67e972`，不打 tag，不触发正式 release。
2. 被审任务保持 `in_progress/review`，Reviewer 不执行 apply/close。
3. 修复顺序：partial CAS 元数据和 cache-hit 保护 → ParseFact Option/NULL ABI →
   signature/visibility 真对齐 → daemon 测试编译与完整 recovery state machine →
   PyInstaller 单份扩展 → 四平台 R11 证据。
4. 下一轮必须继续使用 detached clean commit 和真实冻结产物复审，不能用
   `cargo check`、单元测试数量或文档声明替代生产链证据。
