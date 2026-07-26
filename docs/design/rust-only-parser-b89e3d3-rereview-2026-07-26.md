# Rust-only Parser b89e3d3 整改独立复审

> 日期：2026-07-26
> 复审任务：`T-1785074765575-9613b1e2`
> 被审提交：`b89e3d3e45020fdd97f4bb20663865949a06d97e`
> 结论：**拒绝 apply/close、推送和打 tag；仍有 3 个 P0 + 3 个 P1**

## 1. 结论

本轮在 detached clean worktree 中重新构建 Rust wheel、运行 parser gate 和完整
daemon 测试，并用隔离 wheel 构建 Windows PyInstaller 产物。不能接受“4 个 P0 +
3 个 P1 阻塞项全部修复”的总体声明。

已经独立确认的修复：

1. R13 的 partial CAS 不再被 `lookup()` 当作 ready 二次命中；同一畸形输入连续
   两次均返回 `partial_published`。
2. R12 的 daemon 测试编译错误已修复，完整 daemon 模块测试 416/416 通过。
3. R17 的 PyInstaller 双份扩展问题已在真实 Windows 产物中修复；三项 frozen
   smoke 和 inspector 真实 Rust 加载均通过，bundle 只有一份扩展。
4. R14 的 parser JSON 已使用 1-based `local_id` 和 Option/NULL parent/caller。

但是 R14 没有把同一 ABI 保持到持久化 CAS，R15 的 16 语言门禁仍是假覆盖，R16
会在 generation 尚未进入可查询 snapshot 时把 retry 条目标为 applied。R11 也仍
没有四平台发布证据。

## 2. P0 阻塞项

### P0-1：R14 的 1-based ParseFact ID 写入 CAS 后重新变成 0-based

`CasSymbolInput` 没有 `local_id` 字段
（`rust_ext/src/daemon/cas.rs:182-200`）。`parse_result_to_cas_input()` 虽转发
`lexical_parent_local_id`，却丢弃 `SymbolInfo.local_id`
（`rust_ext/src/daemon/replicator.rs:706-739`）。最终 publish 按 vector 下标写
`local_symbol_id=i`（`rust_ext/src/daemon/cas.rs:500-518`）。

独立 file-backed CAS probe 使用真实 `_daemon_parse_and_publish()` 解析嵌套函数，
数据库结果为：

```text
[("outer", 0, NULL), ("inner", 1, 1)]
```

parser JSON 的正确值本应是 outer=1、inner=2、inner.parent=1。持久化后 inner 的
parent 指向了它自己，后续 projection、调用边解析和图查询都会使用错误身份。现有
R14 测试只检查 parser JSON，没有读取发布后的 `cas_symbols`。

### P0-2：R15 gate 声称覆盖 16 语言，实际字段对齐只覆盖 11 语言

signature/visibility alignment 参数来自
`tests/test_p31_multi_lang.py:330-342` 的 `_LANGUAGE_SAMPLES`，其中只有 Python、
Rust、Go、Java、TypeScript、JavaScript、Ruby、PHP、Scala、C#、C++。Kotlin、
Swift、HCL、Elixir、C 均未进入该字段对齐测试。

但 `tests/parser_contract/gate_report.py:390-394` 无条件把 16 种语言写入
`languages_covered`。clean wheel 的 gate 因而报告：

```text
overall_status=pass
775 passed / 0 skipped
languages_covered=16
```

这不是实际覆盖率。仓库中的 `_probe_sig.py` 只是一次性脚本，不是 pytest/CI
fail-closed gate。真实 C 生产入口 `parse_c_file()` 的函数、struct 和 enum builder
仍在 `rust_ext/src/lib.rs:565-577,609-621,650-662` 写
`signature: String::new()`。独立 golden C probe 得到 add/main 两个函数的
signature 都为空。R15 只修复了被参数化的 11 种语言，不能宣称 16 语言
signature/visibility 对齐完成。

### P0-3：R16 恢复链会提前清账，失败 generation 仍可能永远不可查询

daemon 启动先执行 `recover_all_workspaces()`，之后才创建共享
`SnapshotCachePublisher`
（`rust_ext/src/bin/cw_daemon.rs:185-193,221-246`）。启动恢复代码也明确说明只做
CAS publish、不能发布 snapshot，却在 CAS 成功后立即
`parse_retry_log.mark_applied()`
（`rust_ext/src/bin/cw_daemon.rs:944-1016`）。这会永久移除尚未 merge、committed、
replicate 和 publish snapshot 的 retry 条目。

显式 `workspace.recover` 也没有闭合：

1. 它只在 `self.resources.get(&ws_id)` 命中时重放 retry
   （`rust_ext/src/daemon/workspace.rs:1996`），没有调用已有的
   `get_or_init_resources()`。真实重启后每个 worker 的 resources map 都是空的，
   RPC 可能直接跳过 retry；现有测试在调用前手工向 map 插入 resources
   （`:3255-3290`），没有覆盖真实路径。
2. merge 失败、workspace ID 为 0、generation CAS 返回 false、replicate 失败等
   情况下，代码仍无条件执行 `mark_applied`
   （`:2073-2207`）。注释声称“失败回滚”，实际只回滚 committed，不保留 retry。

因此 R16 不是“完整状态机”，而是存在明确的恢复数据丢失窗口。

## 3. P1 问题

### P1-1：partial CAS 永久残留，GC 不处理新状态

R13 新增的 `state='partial'` 不会被 ready lookup 命中，这是正确修复。但两个 GC
入口都没有删除 partial 父行：

- 精确 GC 只删除 `state='ready'`，另清理 `state='building'`
  （`rust_ext/src/daemon/cas.rs:678-706`）；
- grace GC 的 stale 集也只选择 `state='ready'`
  （`:756-766`）。

畸形编辑的每个新 content hash 都可能留下永久 `cas_file_cache` partial 行；精确
GC 还会先删其子事实，使父行成为不可回收空壳。需要定义 partial TTL/引用语义并纳入
两种 GC。

### P1-2：R17 产物修复成立，但声称的 35 个 inspector 测试全绿不成立

真实 Windows bundle 只有一份 `callwarden_core`，因此 R17 的生产目标已经达到。
但当前提交执行：

```text
python -m pytest tests/test_release_bundle_inspector.py -q
34 passed, 1 failed
```

失败测试 `tests/test_release_bundle_inspector.py:427-447` 在整个 spec 中匹配独立行
`'callwarden_core',`，把
`release/pyinstaller/callwarden.spec:91-100` 的 `_common_excludes` 条目误判为
hiddenimports。实现方给出的“35 passed”无法在 clean commit 上复现，验收代码必须
按实际列表作用域解析。

### P1-3：R11 四平台发布证据仍未完成

实时远端检查：

```text
origin/master = 9409ede
HEAD ahead 11 commits
latest tag = v0.3.2
```

没有新 tag，也没有 macOS arm64、Linux x86_64、Linux aarch64、Windows 的
before/after 体积表。把 R11 安排在独立复审之后是合理顺序，但它仍是 open，不应在
“3/3 P1 已修复”总数中计为完成。

## 4. 验证记录

| 验证 | 结果 |
|---|---|
| detached clean HEAD / `git show --check` | PASS，`b89e3d3` |
| `cargo check` | PASS，23 warnings |
| 完整 daemon lib tests | PASS，416 passed |
| isolated Rust wheel | PASS |
| parser gate | 表面 PASS，775 passed；实际字段对齐样本仅 11 语言 |
| partial 二次命中 probe | PASS，两次均为 `partial_published` |
| file-backed CAS identity probe | **FAIL，outer 被写为 ID 0，inner parent 自指** |
| C signature probe | **FAIL，函数 signature 为空** |
| Windows PyInstaller build | PASS |
| frozen 三项 smoke | PASS，3/3 |
| bundle inspector + Rust 真加载 | PASS |
| bundle core 数量 | PASS，1 份，36,238,848 bytes |
| inspector 单测 | **FAIL，34 passed / 1 failed** |
| 四平台 CI / tag / size report | 未执行、未填充 |

## 5. Reviewer 决定

1. `b89e3d3` 不得 apply/close、推送或打 release tag。
2. 被审父任务保持 `in_progress/review`，Reviewer 不替实现方修复或关闭。
3. 下一轮顺序：保持 ParseFact local ID 到 CAS → 16 语言真实字段 gate →
   recovery 只在 snapshot 发布成功后清账 → partial GC → inspector 测试 →
   独立复审 → R11 四平台发布证据。
4. R11 只能在上述阻塞项通过后触发；发布数据必须来自 CI artifact，不能手填。
