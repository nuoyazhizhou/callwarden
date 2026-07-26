# Rust-only Parser R0-R4 整改独立复审

> 日期：2026-07-26  
> 复审任务：`T-1785037113024-a20cc8eb`  
> 被审提交：`de7db9f`  
> 结论：**拒绝 apply/close，仍有 4 个 P0 + 3 个 P1**

## 1. 总结

本轮没有复用实现 Agent 的工作区或已安装扩展作为结论依据，而是在 detached clean
worktree 中重新执行 Rust 构建、隔离 wheel、契约 gate、daemon 全量测试和 Windows
PyInstaller 冻结产物 smoke。

已经确认修复的部分：

1. `de7db9f` 是 clean commit，`git show --check` 通过。
2. detached clean HEAD 的 `cargo check` 通过，R0 的 `LangConfig` 编译阻塞已修复。
3. gate 的统计层已改为 Rust 不可用、任一 skipped、任一 total=0 均 fail closed。
4. daemon refresh 外层已调用 generation guard，metrics 已进入 health。
5. inspector 已能识别带 ABI 后缀的 `callwarden_core*.pyd/.so`。

但是生产调用链、ABI 语义和真实发布产物仍未闭合，不能接受“5 P0 + 4 P1 已全部
闭环”的声明。

## 2. P0 阻塞项

### P0-1：Rust-only 冻结包仍无法启动

`db/db_base.py:20` 仍在模块顶层执行：

```python
from ..parsers import ModuleResolver, CallResolver
```

与此同时 `release/pyinstaller/callwarden.spec:358` 明确排除整个
`callwarden.parsers`。`_try_init_rust_parser()` 的 try/except 位于
`CodeGraphDB.__init__`，无法捕获更早发生的模块顶层导入失败。

在 clean `de7db9f` 上用隔离构建的 Rust wheel 完成 PyInstaller 后，三个发布 smoke
全部失败：

```text
cw.exe --version                 exit 1
cw.exe --help                    exit 1
cw.exe server --check-imports    exit 1

ModuleNotFoundError: No module named 'callwarden.parsers'
```

因此 R2 只把 `tree_sitter` 改成延迟导入，没有真正断开 frozen 生产入口对
`callwarden.parsers` 包的依赖。

### P0-2：结构化 parse diagnostics 没有进入 CAS 发布判定

`rust_ext/src/multi_lang.rs:1315-1336` 已实现 `parse_status_from_result()` 和
`parse_diagnostics_from_result()`，但编译器确认两者均未使用。

真实 CAS 路径 `rust_ext/src/daemon/replicator.rs:584-592` 仍只检查旧字段
`parse_result.error`，没有读取 `parse_result.diagnostics`。畸形 Rust 源码
`fn broken( {` 的隔离实测结果为：

```json
{
  "error": null,
  "diagnostics": {
    "status": "partial",
    "syntax_error_count": 1,
    "partial_parse": true
  }
}
```

按当前代码它仍会继续 `publish()` 并返回 `ready_published`，随后外层 guard 把它视为
成功。`replicator.rs:1890-1911` 的“parse failure”测试实际上使用空文件，并明确断言
`ready_published/ready_cache_hit`，没有覆盖语法错误或 partial generation。

这意味着坏解析结果仍可污染 CAS 并替换最后一个好 snapshot。

### P0-3：ParseFact local ID 重新引入已修过的哨兵冲突

企业设计 `enterprise-phase1-phase3-detail.md:1074-1077` 明确规定：

- `local_id` 从 1 开始，0 保留给 synthetic module symbol；
- `lexical_parent_local_id` 用 NULL；
- `caller_local_id` 用 NULL 表示顶层裸调用。

当前实现却在 `rust_ext/src/lib.rs:261-266,274-334` 使用 0-based `local_id`，
`lexical_parent_local_id=-1`，并同时用 `caller_local_id=0` 表示未解析调用者。
对应测试 `tests/parser_contract/test_identity_range.py:636-640,751-752` 还把这个冲突
编码成了预期行为。

隔离真实解析中，第一个函数 `first` 的 `local_id=0`，函数体内 `helper()` 的
`caller_local_id` 也是 0；它与“未解析调用者”无法区分。该值随后被
`parse_result_to_cas_input()` 原样写入 CAS，属于图谱身份语义错误。

### P0-4：契约 gate 统计 fail closed，但契约内容仍是假绿

标准目录名的 clean checkout 配合隔离 wheel 时，gate 报告 `786 passed / 0 skipped`。
这个结果不能证明 16 语言 ParseFact 契约已经对齐：

- `tests/parser_contract/test_golden_fixtures.py:1-5` 明确声明只检查 JSON 结构，
  不比较当前 Rust parser 输出。
- `rust_ext/src/multi_lang.rs:914` 仍把所有通用语言 signature 写为空字符串。
- `multi_lang.rs:925-940` 除 PHP 外基本把 visibility 默认成 `public`。
- `tests/test_rust_python_alignment.py:708-709` 只比较双方都有非空 signature 的项，
  随后 `:722-747` 又要求 Rust signature 全空才通过。
- `tests/parser_contract/baseline.json` 仍逐语言记录
  `signature_missing_in_rust`。

因此 R1 修复了 skipped/total 的统计漏洞，却没有完成上一轮要求的真实 golden
输出比较、signature/visibility 契约和 0 个有效比较项的字段级门禁。

## 3. P1 问题

### P1-1：retry log 只写不重放，ParserDoctor 仍未进入生产路径

`workspace.rs:1296-1316` 会把允许重试的失败追加到 `parse_retry.log`，health 也会统计
pending 数量。但 `parse_retry_log::replay_pending()` 只在自身单元测试中调用。

daemon 启动的 `recover_all_workspaces()` 和 RPC `workspace.recover` 都只处理
`staging.log`，从未读取、重试或 mark applied `parse_retry.log`。此外
`ParserDoctor` 仍无生产调用，`ParserMetrics` 的 latency 固定记录为 `0.0`。

所以 R3 的“daemon 崩溃后重放失败 generation”和 doctor 可观测性尚未实现。

### P1-2：artifact inspector 仍不能完成真实加载，也不拒绝重复 Rust 扩展

ABI 后缀匹配已经修复，但 `release/inspect_pyinstaller_bundle.py:414-421` 仍以模块名
`callwarden_core_verify` 加载 PyO3 扩展，真实执行 `--verify-rust-parse` 得到：

```text
dynamic module does not define module export function
(PyInit_callwarden_core_verify)
```

同一个冻结包还同时包含：

```text
_internal/callwarden_core.cp314-win_amd64.pyd                   36,172,800
_internal/callwarden_core/callwarden_core.cp314-win_amd64.pyd  36,172,800
```

总目录 137.21 MiB，其中两份 Rust 扩展共 72.35 MB，占 50.28%。普通 inspector
仍返回 exit 0，没有把重复扩展或不可执行的 `cw.exe` 判为失败。新增的 32 个单元测试
只覆盖文件名 helper 和占位文件，没有执行真实 PyO3 load、冻结入口 smoke 或重复检查。

### P1-3：四平台真实 CI 和 before/after 体积证据仍不存在

复审时远端状态仍是：

```text
origin/master = 9409ede
latest tag    = v0.3.2
```

`de7db9f` 尚未推送，也没有新 tag，因此四平台 workflow 不可能验证本次整改。
`docs/design/rust-only-parser-size-report.md:94-143` 的 Windows、macOS、Linux x86_64、
Linux aarch64 before/after/delta 仍全部是“待填充”。

本机 Windows 冻结产物已经证明当前提交不可发布，不能先推送或打 tag触发正式发布。

## 4. 验证记录

| 验证 | 结果 |
|---|---|
| clean HEAD / `git show --check` | PASS |
| detached `cargo check` | PASS，23 warnings |
| 隔离 Rust wheel 构建和 import | PASS |
| parser gate（标准 checkout 名） | PASS，786 passed / 0 skipped，但契约内容不完整 |
| parser gate（任意 worktree 名） | FAIL，根 `__init__.py` collection error |
| daemon 完整模块测试 | PASS，411 passed |
| malformed Rust diagnostics | `error=null`, `status=partial`, `syntax_error_count=1` |
| Windows PyInstaller build | PASS |
| frozen 三入口 smoke | **FAIL，3/3 均缺 `callwarden.parsers`** |
| inspector 普通模式 | 错误 PASS |
| inspector `--verify-rust-parse` | **FAIL，PyInit 名称错误** |
| frozen bundle | 137.21 MiB，Rust 扩展重复两份 |
| remote/tag/四平台证据 | 无 |

## 5. Reviewer 决定

1. 被审父任务 `T-1784986236712-736b2331` 不得 apply/close。
2. 本复审任务只提交审查结果，不替实现任务关闭问题。
3. 修复顺序应为：冻结入口 P0 → diagnostics/CAS P0 → local ID ABI P0 →
   真实 golden/signature/visibility P0 → retry recovery/inspector/四平台证据。
4. 下一次送审必须提供一个 clean commit；Reviewer 会重新独立构建 wheel 和 frozen
   bundle，不接受复用开发环境中的根级 `.pyd` 或仅引用单元测试结果。
