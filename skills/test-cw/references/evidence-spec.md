# 证据规范（`evidence/<AC-id>.md`）

> 一条 AC 一份文件。**八段缺一即该 AC 未通过**——不是格式洁癖：缺第 5 段，判据 ② 就无法核对；缺第 2 段，命令不可复现；缺第 7 段，读者不知道它为什么算过。

## 1. 八段结构

| # | 段 | 必填内容 | 常见错法 |
|---|---|---|---|
| 1 | 头部 | AC-id / REQ-id / 标题 / task_id / `run_revision` / `code_revision` / branch / 时间（带时区） | 只写日期不写时区；漏 `code_revision` |
| 2 | 环境指纹 | OS+arch；Python 可执行文件与 `-V`；`pytest --version`；`rustc -V` + `cargo -V`（涉及 Rust 时）；`CW_TEST_MODE`；`PYTHONPATH`；`CARGO_TARGET_DIR` 实际值；依赖来源 | 写"本机环境"了事 |
| 3 | 命令原文 | 可复制执行的完整命令行（含 `--junitxml` 路径、nodeid 列表或 `-k` 表达式、cwd） | 写"跑了相关测试" |
| 4 | 统计 | collected / passed / failed / error / skipped / xfail / deselected + `rc` | 只写"全绿" |
| 5 | **必需 nodeid 完成表** | 一行一个 `nodeid → outcome`，**全列，含失败的** | 只列通过的（等于隐藏证据） |
| 6 | 关键输出 | 失败 traceback 首尾、断言消息、关键日志行；≤20 行；脱敏 | 贴全量日志 / 贴无关 INFO |
| 7 | 判读 | `PASS\|FAIL\|BLOCKED\|N/A` + 一句话理由 + **指向判据哪一条**（如"卡在 ④ skipped=2"） | 只写"通过" |
| 8 | 附件指针 | `reports/junit-<AC>.xml` 相对路径（+sha256 可选） | 指向 `/tmp` 下会被清理的路径 |

## 2. 生成方式（强制）

**骨架由脚本从 junit 生成，人工只写第 7 段。** 手抄 nodeid 必错，漏一行判据 ② 就失效且不可察。

```python
# 参考实现：解析 junit → 每个 AC 的 nodeid 完成表
import xml.etree.ElementTree as ET
root = ET.parse("reports/junit-<batch>.xml").getroot()
for tc in root.iter("testcase"):
    outcome = "passed"
    for ch in tc:
        if ch.tag == "failure": outcome = "failed"
        elif ch.tag == "error": outcome = "error"
        elif ch.tag == "skipped":
            outcome = "xfail" if (ch.get("type") or "").startswith("pytest.xfail") else "skipped"
    print(f'{tc.get("classname")}::{tc.get("name")}\t{outcome}')
```

注意三个坑：

1. `skipped` 里要区分 `pytest.xfail`（→ 记 `xfail`），否则 xfail 会被误记成 skip，判据 ④ 的分母就错了；
2. `collection_errors` 不在 `<testcase>` 里，要看 `<testsuite errors=…>` 或 `--collect-only` 的退出；
3. 同一 nodeid 参数化后是多条 testcase，第 5 段要**逐条列**，不能折叠成一行。

### 2.1 Rust 批次（cargo test）

junit 不由 pytest 产生，由 `scripts/cargo_test_evidence.py parse` 从 libtest stdout 生成等价产物（testsuite name=`cargo-test-<target>`，nodeid 形态 `<target>#<test_path>`）：

```powershell
cargo test --<target> -- --list                 # 采集集：list 模式 → nodeid 全集
cargo test --<target> -- --exact <ids...> 1> run.txt 2> run.err   # 执行；rc 一并记录
python scripts/cargo_test_evidence.py parse --input run.txt --target <target> --rc <rc> `
    --required required.txt --junit reports/junit-<batch>.xml --out 骨架.md `
    --command <命令原文> --stderr run.err
```

统计语义差异（对照第 4 段字段）：

| 本 schema 字段 | Rust 侧取值 |
|---|---|
| collected / executed | 汇总行 `passed+failed+ignored+measured`（实际执行数） |
| skipped | libtest `ignored`（libtest 无 xfail 概念；`#[ignore]` 即 skip，进判据 ④ 分母） |
| xfail | 恒为 0（概念不存在） |
| deselected | libtest `filtered out`（备查；判据 ⑤ 用「required 缺席」判定） |
| collection_errors | rc≠0 且无任何 `test` 行（编译失败），脚本合成 `<target>#<collection>` error testcase |

Rust 批上限 **≤300 ids**（Windows 命令行限制），超限拆批；多二进制（lib + 各集成测试）逐二进制分批，不得混判。详见 `../DESIGN.md §15`。

## 3. 三个示例

### 3.1 PASS（真实样例，摘自 backlog-46 试跑）

```markdown
# AC-03 · semgrep scan 各分支真失败（success=false）→ rc=1，不再静默 rc=0

- 结论：PASS
- 关联：REQ-46.2
- 命令：`PYTHONPATH=C:/git_work CW_TEST_MODE=1 C:/Python314/python.exe -m pytest tests/test_report_rc_semantics.py tests/test_i18n_key_existence.py tests/test_i18n_placeholder_consistency.py tests/test_cli_reference_sections.py --junitxml=.design/<feature>/reports/junit-dryrun.xml -q`
- 环境：Windows 11 x64 · CPython 3.14.3（C:/Python314/python.exe）· pytest 9.0.2 · rustc 未涉及 · CW_TEST_MODE=1 · PYTHONPATH=C:/git_work · CARGO_TARGET_DIR 未设置
- run_revision：`c10a1e5`（HEAD）· code_revision：`f8b8588` · 两者间相关文件 `git diff --stat` 为空
- 用例统计：collected=149 passed=149 failed=0 error=0 skipped=0 xfail=0；本 AC required=3 matched=3，required 集 skip/xfail=0
- 隔离：known-red 未使用
- 证据时间：2026-11-22 09:25 +08:00

## 必需 nodeid 完成表

| nodeid | outcome |
|---|---|
| `test_report_rc_semantics::test_semgrep_scan_incremental_failure_exit1` | passed |
| `test_report_rc_semantics::test_semgrep_scan_save_failure_exit1` | passed |
| `test_report_rc_semantics::test_semgrep_scan_default_failure_exit1` | passed |

## 判读

三个失败分支各自独立覆盖，属负向用例；mutation 探针下它们保持绿，说明与 flag 语义解耦。
判据落点：① 双 revision 一致且零漂移 → ② required ⊆ junit 全 passed → ③ 批内 0 失败 → ④ required 集 skip/xfail=0 → ⑤ 无 deselect → ⑥ rc=0 且 collected=149>0。
```

### 3.2 FAIL

```markdown
- 结论：FAIL
## 必需 nodeid 完成表

| nodeid | outcome |
|---|---|
| `test_report_rc_semantics::test_issues_findings_with_flag_exit3` | failed |
| `test_report_rc_semantics::test_issues_findings_without_flag_rc0` | passed |

## 关键输出

```
E   AssertionError: assert 0 == 3
E    +  where 0 = _exit_code(...)
tests/test_report_rc_semantics.py:68: AssertionError
```

## 判读

卡在判据 ②（required 未全 passed）。根因初判：`_exit_on_findings` 的触发条件被反转（mutation 探针复现），
已建 `T-xxxx`（`action:"fix"`，首行 REQ-46.1）。
```

### 3.3 N/A（只在执行前成立）

```markdown
# AC-07 · 活体验证：`cw detect-cycles` 不带 flag → rc=0

- 结论：N/A（manual）
- 合法依据：`test-plan.md` 在执行前已标 `level=manual`，并写明人工验收方式（见下）
- 未执行：本轮为自动化批跑，不触达 daemon / 真实 workspace

## 人工验收方式

1. 在已绑定 workspace 的仓库根执行 `cw detect-cycles`（不带 flag）；
2. 记录退出码；期望 `0`；为 `3` 则判 FAIL 并建 P1 单。

## 判读

本文件只证明「尚未验证」，不证明成立。需求关闭判据把本 AC 视为**未绿**。
```

## 4. 禁止写入

| 禁止 | 原因 |
|---|---|
| token / 密钥 / 连接串 / 真实用户名 / 绝对私有路径 | 证据要进版本库 |
| 全量日志、整文件 diff | 噪声掩埋关键行，读者找不到判据落点 |
| 未执行的计划语句（"预计/将验证/待补充"） | 计划不是证据 |
| 他人 evidence / 上一轮 rc 与统计 | 用历史运行冒充本轮 = 伪造 |
| 推测性结论（"应该是环境问题"） | 推测写在「判读」里也要标明是初判，不能写成事实 |
| 把 skip / xfail 写成 pass | 判据 ④ 直接失效 |
| 被清理的临时路径（`/tmp/...`） | 证据必须自包含，半年后仍可判读 |

## 5. 与 ac-map 的联动

- `ac-map.md` 的「证据位置」只指向本目录文件；**空值或指向不存在的文件 = 该 AC 未通过**。
- AC 变更（revision+1）时：旧证据保留不删，新证据用 `evidence/<AC-id>-r2.md`；`ac-map.md` 指向最新一份。
- 需求关闭判据 = **快照内**每条 AC 的结论均为 PASS（N/A 不算绿），且证据已提交。
