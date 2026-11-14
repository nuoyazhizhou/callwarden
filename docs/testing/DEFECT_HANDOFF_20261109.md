# CallWarden 缺陷登记表（Handoff Report）

> 生成时间：2026-11-09 ｜ 审计基线 HEAD：`7e178cb`（源）/ 本地提交 `4133187`（含账本对齐）
> 门禁裁决：**红，不能发**。本表列出全部阻断项与已知未知，供分派给其他 agent 修复。
>
> **如何使用**：每条缺陷自包含。①「修复指引」给具体改动点；②「验收标准」给可判定的 done 条件；③「建议路由」给可派发的 agent 域。修复后请在对应条目勾选并回填 PR/commit。

---

## 0. 门禁裁决摘要

| 门禁腿 | 状态 | 说明 |
|---|---|---|
| 静态清单自洽（`gen_test_cases.py --check`） | 🟢 | 三账本与源 HEAD 字节自洽 |
| 提交树 CI 比对（远端） | 🔴 | 远端账本滞后，未 push |
| CRLF 稳定性 | 🔴 | `.gitattributes` 缺 `eol=lf`，反复翻转 |
| 团队终审（`full_stack_review_20261024.md`） | 🔴 | 结论**不通过**，T-1~T-12 全开 |

**缺陷分级**：P0 ×10（DEF-01、DEF-02、DEF-03、T-1~T-9）、P1 ×3（T-10~T-12）、已知未知 ×4（不分级但不可瞒）。

---

## 1. 缺陷总表

| ID | 标题 | 级别 | 建议路由域 | 状态 |
|---|---|---|---|---|
| DEF-01 | 提交树账本 CI false-fail（远端未同步） | P0 | CI/Infra | 本地已修，待 push |
| DEF-02 | CRLF 根因（`.gitattributes` 缺 `eol=lf`） | P0 | CI/Infra | **DONE**（a8002cc：三账本 eol=lf + renormalize，2026-11-11） |
| DEF-03 | GAP-15 治理语义冲突 | P0 | Governance/Rust | **DONE**（2026-11-11 用户签字：源码为准；Adjudicator v4 模板 §29/§32 已对齐，迁移版 role-protocol.md 原本已对齐，一致性测试 test_adjudicator_returned_handoff_reopens_executor_remediation_atomically 在位） |
| T-1 | KR-C nightly 验收门零证据 | P0 | CI/Acceptance | **DONE**（登记过期：semgrep 已移 extras，KR-C closed，known_red.json 有 nightly 绿+Gate 证据） |
| T-2 | Gate 1/2 汇总行污染（反篡改门恒红） | P0 | CI/Gate | OPEN |
| T-3 | known-red 零隔离 | P0 | CI/Quarantine | **DONE**（登记过期：ci.yml 已有 deselect step + quarantine job，KR-A/B/C 全 closed） |
| T-4 | KR-A 83 条 residue | P0 | Security/Authority | **DONE**（登记过期：KR-A closed——audit passed=true/finding_count=0/transition=79/sunset 未过期，migrate.py 已退役，MASKING 守卫 tests/test_srv_019.py:144 在位） |
| T-5 | 写路径无 workspace 根约束（越权写） | P0 | Security | OPEN |
| T-6 | bandit 门禁范围外 75 条 | P0 | Security | **DONE**（登记过期：bandit 范围已含 db/ scripts/ cw.py config.py，75 条逐条处置入注释） |
| T-7 | Rust 集成测试只编译不执行 | P0 | Rust/CI | **DONE**（登记过期：rust-unit-test job 已有 cargo test --tests 执行步） |
| T-8 | E2E 结果 advisory 不阻断 | P0 | CI/E2E | DONE 本地（e616a90，2026-11-11：按触发形态阻断；tag run 验证待 CI） |
| T-9 | windows-arm64 产物零 E2E | P0 | CI/E2E | DONE 本地（3e773af：新增 e2e-verify-windows-arm64.yml + cryptography 46.0.3 约束；tag run 验证待 CI） |
| T-10 | Windows Python 全量零执行 | P1 | CI/Python | DONE 本地（33f9e32：test-windows job，windows-latest × py3.14 + junction；3 连绿验收待 CI） |
| T-11 | 真空伪测试 `test_uid_isolation` | P1 | Security/Test | **DONE**（1c2533c：真空双 UID 隔离实现，2026-11-11） |
| T-12 | 门禁脚本零单测 | P1 | CI/Gate | OPEN |
| GAP-16 | skip 站点 root_cause 0/456 复核 | 已知未知 | Audit | OPEN |
| D-MATRIX | D 专项矩阵未写 | 已知未知 | Test Design | OPEN |
| DEBT-LEDGER | 技术债账本 4 项漏登 | 已知未知 | Audit | OPEN |
| STASH-0 | 原 `stash@{0}`(review-edits-2026-10-28) 已清除；2026-11-14 核实存在新 `stash@{0}`(e741-prod-wip)，内容与本次审计无关 | 已知未知 | Repo Hygiene | OPEN（原条目销项，新 stash 待 owner 处置） |

---

## 2. 缺陷详情（A 类：审计/CI 基础设施）

### DEF-01　提交树账本 CI false-fail（远端未同步）　【P0】

- **现象**：CI 检出真实 HEAD 跑 `python docs/testing/gen_test_cases.py --check` 必报 `Generated content differs` 而红。
- **根因**：提交树 `docs/testing/STATIC_AUDIT.json` 的 `supplied_source_revision` 记录的是旧值 `dcd99c7f`，而真实源 HEAD 已是 `7e178cb`。账本内容与扫描树不一致即触发字节比对失败。
- **证据**：`git show HEAD:docs/testing/STATIC_AUDIT.json` → `"supplied_source_revision": "dcd99c7f…"`（远端 HEAD `7e178cb`）；本地提交 `4133187` 已将其改为 `7e178cb`。
- **当前进度**：本地账本已对齐；**但 `4133187` 未 push**，故远端账本仍滞后、远端 CI 仍红。
- **修复指引**：
  1. 将本地 `4133187` **push** 到远端（治标，立即闭合远端 CI）；
  2. 制度性修复：任何重生成账本的提交，必须保证 `supplied_source_revision == 真实源 HEAD`，并配合 DEF-02 的 `eol=lf`，使 `--check` 在干净 checkout 下稳定 PASS。
- **验收标准**：远端 CI 在 `7e178cb`（及之后每个含账本的提交）跑 `--check` 全绿；提交树账本 `revision` 字段 == 真实源 HEAD。
- **建议路由**：CI/Infra agent。

### DEF-02　CRLF 根因（`.gitattributes` 缺 `eol=lf`）　【P0】

- **现象**：三份机器账本在日常 `git checkout/reset/stash pop` 时 LF 被翻成 CRLF，导致 `--check` 字节比对失败（已多次实锤，≥4 次）。
- **根因**：Windows `core.autocrlf=true` 下，`docs/testing/STATIC_AUDIT.json`、`SKIP_SITES.json`、`TEST_CASES.md` 未被 `.gitattributes` 钉 `eol=lf`。生成器写 LF，但 git 触碰时重写为 CRLF。
- **证据**：提交/状态时报 `warning: ... LF will be replaced by CRLF the next time Git touches it`（三账本）。
- **修复指引**：
  1. 在 `.gitattributes` 增加三行：`docs/testing/STATIC_AUDIT.json eol=lf`、`docs/testing/SKIP_SITES.json eol=lf`、`docs/testing/TEST_CASES.md eol=lf`；
  2. `git add --renormalize docs/testing/` 重新归一，提交使索引与工作树一致；
  3. 验证：`git checkout` 三账本后 `open(f).read().count(b'\r\n') == 0`。
- **验收标准**：从干净状态 `git checkout` 三账本后 CRLF=0；随后 `--check` 仍 PASS；警告不再出现。
- **制度性加固（2026-11-14，根治残留路径）**：`.gitattributes eol=lf` 只在 checkout/add 生效，上游 CRLF 工作树的存量未 renormalize 文件重生账本时仍会使输入哈希整体漂移。`gen_test_cases.py` 的 `digest()` 已改为 **LF 归一化后哈希**（输入哈希 EOL 无关，与 `read()` 通用换行语义一致），`validate_skip.py` 磁盘对拍同步归一化；此后任何平台、任何行尾状态的工作树重生账本均得同一哈希。
- **建议路由**：CI/Infra agent。

### DEF-03　GAP-15 治理语义冲突　【P0，需人类签字】

- **现象**：`adjudicator_returned` 状态在源码与协议文档中语义相反，治理验收无法冻结。
- **根因**：`rust_ext/src/daemon/task_collab_lifecycle.rs:1516-1524` 在 `adjudicator_returned` 分支**同一事务里准备 `fix_defect`**（remediation_plan），而协议声称「returned 不自动追加整改 step、不自动 reopen——自动 remediation 仅 `reviewer_blocked` 触发」。
- **证据**：
  - 源码：`task_collab_lifecycle.rs:1516-1524`（含 `outcome == "adjudicator_returned"` → 准备 `fix_defect`）；
  - 协议：`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Adjudicator v4.md:32`（returned 不自动追加/不 reopen）；
  - 迁移位置：`.agents/skills/cw-task-loop/references/role-protocol.md`。
- **修复指引**：
  1. **架构师/业务 owner 最终签字冻结权威语义**：returned 到底是否触发自动 remediation；
  2. 据此让源码与协议二选一对齐；
  3. 加一致性测试（projection/steps/assignment/provenance/幂等）。
- **验收标准**：源码与协议不再矛盾；存在冻结的权威语义文档 + 一致性测试绿。
- **建议路由**：Governance/Rust agent（仅能准备对齐补丁，**签字需人类**）。
- **注意**：此条非纯技术缺陷，agent 不可自行闭合，必须回交签字。

---

## 3. 缺陷详情（B 类：团队终审 T-1~T-12）

> 来源：`docs/testing/full_stack_review_20261024.md` §4。以下为可直接派发的简版；完整论证见原文档。

### T-1　KR-C：nightly 验收门零证据　【P0】
- **现象**：`acceptance-gates` nightly 自 2026-10-19 起 ≥5 连挂，均止于依赖安装，无全 profile 验收证据。
- **根因**：`pyproject.toml:28`（semgrep 核心依赖）与 `acceptance-gates.yml:131`（windows×py3.14 runner）冲突。
- **修复**：semgrep 改 `extra`+懒加载或拆两步安装；修后 `workflow_dispatch` dry_run 实跑。
- **验收**：连续 2 次 nightly 绿 + Gate verdict 存档。

### T-2　Gate 1/2 汇总行污染（阻断）　【P0】
- **现象**：反篡改门从未通过，`manifest` 与源码完全一致也 `exit 2`。
- **根因**：`cicd/check_selector_manifest.py:63-67` 的 `_load` 把 pytest 汇总行（如 `"114 tests collected in 0.45s"`）当未登记 selector。
- **修复**：`_load` 加 nodeid 形态过滤（含 `::`，剔除 `^\d+ tests? collected`）；两个门禁脚本补单测。
- **验收**：修复后本机 collected 跑 `exit 0`、删一条 manifest 跑 `exit 2`；单测全绿。

### T-3　known-red 零隔离　【P0】
- **现象**：KR-A/KR-B 共 9 红仅文档登记，CI 无隔离 ⇒ 每次 push 主 test job 恒红。
- **根因**：`ci.yml:168-180` 无 deselect/quarantine。
- **修复**：建 `docs/testing/known_red/` 登记表（编号/根因/owner/关闭条件/**到期日**）+ quarantine job（deselect 出主门、单独报数、到期升级红）。
- **验收**：主 job 绿 + quarantine job 按预期红。

### T-4　KR-A：83 条 residue　【P0】
- **现象**：audit `passed=false`、4 用例红，83 条 residue 未治理。
- **根因**：生产侧搬迁导致断言位点失效（`tests/test_srv_019.py:72` 断言正确）。
- **修复**：方案 C（第二类过渡声明 + 冻结基线守卫 + 日落；migrate 单独关闭）。
- **验收**：见原文档 §3 MASKING 自检；恢复「零可执行 Python authority」不变量不削弱。

### T-5　写路径无 workspace 根约束（安全严重）　【P0】
- **现象**：越权写可达——`../escape_rel.txt` 与 workspace 外绝对路径均 `status=applied` 落盘。
- **根因**：`db/db_edit.py:113-126` 的 `_resolve_abs_path` 绝对路径原样采用、相对路径不拒 `..`（对照组 `db/db_lsp.py:82-106` 有校验；fuzz 缺口 `tests/test_fuzz.py:152-203`）。
- **修复**：解析后 `commonpath`/`is_relative_to` 校验 + 显式拒 `..` + `realpath` 后再比一次；复用 `_validate_file_path`。
- **验收**：fuzz 增 traversal-write 用例断言 `failed` 且 workspace 外无文件落盘。

### T-6　bandit 门禁范围外 75 条（安全严重）　【P0】
- **现象**：CI bandit 仅扫 `cli/server/cw.py/config.py`；`db/`、`scripts/` 在门外，实跑 75 条 medium+（B608×64、B314×7、B602 HIGH×1 `scripts/split_commit_069.py:15` shell=True、B324 HIGH×1 FP）。
- **根因**：`ci.yml:372` 范围过窄。
- **修复**：`db/` 纳入范围 + `.bandit` baseline（75 条只减不增）；B608 逐条 triage；B314 换 defusedxml；split_commit 改 `shell=False`。
- **验收**：扩面后 `db/` 0 未登记 finding。

### T-7　Rust 集成测试只编译不执行　【P0】
- **现象**：`rust_ext/tests/`（daemon_e2e.rs 全 RPC、p1_f_recovery_e2e.rs kill-9 链 15 例、role_prompt_assets_golden.rs sha256 门）CI 仅 `--no-run --tests` 编译门。
- **根因**：`ci.yml:454-518` 三个执行 job 均 `--lib`+模块过滤，测试不执行。
- **修复**：rust-unit-test job 增 `cargo test --tests`（`CARGO_BIN_EXE` 自动注入）。
- **验收**：连续 3 轮绿。

### T-8　E2E 结果 advisory 不阻断　【P0】
- **现象**：三平台 e2e-verify 退出码非零仅 `::warning::`，产物照传照发。
- **根因**：`e2e-verify-linux-x86_64.yml:181-183`、`e2e-verify-windows-amd64.yml:142-144`、`e2e-verify-linux-aarch64.yml:229-233`。
- **修复**：tag 触发时非零改 `exit 1`；dispatch 形态保留 warning 但 job summary 置顶。
- **验收**：注入失败 E2E（临时改 `EXPECTED_LANGUAGES`）→ tag run 必须红。

### T-9　windows-arm64 产物零 E2E　【P0】
- **现象**：`pyinstaller-build.yml` 构建并发布 arm64 frozen 包，三个 e2e-verify 无对应。
- **根因**：矩阵只命中构建，无验证（`pyinstaller-build.yml:24-27,173-177`）。
- **修复**：新增 `e2e-verify-windows-arm64.yml`（windows-11-arm runner；若不可用则 arm64 显式「构建不发布」并移出矩阵，禁止中间态）。
- **验收**：workflow 存在且 `exit 0`，或矩阵收口 + guard 断言。

### T-10　Windows Python 全量零执行　【P1】
- **现象**：`ci.yml` test job 实为 ubuntu-only；声称的 windows × py3.10–3.14 全量矩阵有误；70 个测试文件引用 win32 分支无 CI 回归网。
- **根因**：`ci.yml:12` 矩阵范围错误（阮清纠正并经源码核实）。
- **修复**：`ci.yml` 增 windows × py3.14 全量 job（沿用 `ci.yml:145-154` 联接+PYTHONPATH 模式）。
- **验收**：连续 3 次 push 绿 + skip 集差异有书面解释。

### T-11　真空伪测试 `test_uid_isolation`　【P1】
- **现象**：ownership UID 隔离用例 body 为 `pass`，Linux root CI 空跑通过——安全关键路径假绿。
- **根因**：`tests/test_phase5_git_clean_dirty.py:397`。
- **修复**：实现真双 UID 隔离（复用 `e2e_workspace_authority_*` harness）或 strict xfail + 跟踪任务，不得留 `pass`。
- **验收**：越权查询注入必须红。

### T-12　门禁脚本零单测　【P1】
- **现象**：`cicd/check_ci_gates.py`/`check_selector_manifest.py`/`gen_coverage_manifest.py` 无直接单测——T-2 缺陷因此存活。
- **修复**：三脚本建单测文件（真实 pytest 输出/JUnit/collected fixture 化，正负向各一组）。
- **验收**：单测入库且绿；后续改门禁先跑单测。

---

## 4. 已知未知（不分级，但不可瞒）

| ID | 内容 | 影响 | 建议路由 |
|---|---|---|---|
| GAP-16 | `docs/testing/SKIP_SITES.json` 456 个 skip 站点 `root_cause_reviewed=True = 0`；零人工根因复核 | 最大「已知未知」：skip 是否真必要无人证 | Audit agent |
| D-MATRIX | `TESTING_PLAN.md` §7 的 D 专项矩阵（G/I/J/E/W/S/A/X 域 + 16 语言正/负/恢复/并发）未写 | 专项验收无基线 | Test Design agent |
| DEBT-LEDGER | 技术债账本 4 项漏登（何序5.2，独立于机器账本） | 技术债不可见 | Audit agent |
| STASH-0 | 原 `stash@{0}`（review-edits-2026-10-28，11 文件）已清除；当前存在新 `stash@{0}`(e741-prod-wip，内容未核) | 冗余、未来 pop 会与已提交内容冲突 | Repo Hygiene |

---

## 5. 当前账本真值快照（派发基线，勿重写除非 HEAD 变）

| 指标 | 值 |
|---|---|
| 源 HEAD | `8e7ee65`（fix(read-1)，本地与 origin 同步） |
| 本地提交（含账本） | 本轮账本/文档对齐改动**未 commit**（见文末基线真值复核） |
| MCP 工具 | 244（`docs/testing/manifests/mcp/mcp_tools.json` 真源） |
| CLI | 86 顶层 / 21 类 / 233 叶子 / extracted 232 |
| Python 定义 / 文件 | 7213 / 697 |
| Rust `#[test]` 属性 / cfg 文件 | 2122 / 121 |
| skip 三机制 | pytest.skip 210 + skipif 222 + importorskip 24 = **456** |
| reason 分布 | 字面量 267 / 表达式 167 / 缺省 22（互斥口径） |
| schema 名称漂移 | 0（GAP-14 闭合） |
| root_cause 复核 | **0 / 456** |
| `--check` | PASS（工作树） |

> 派发提示：其他 agent 修改源码后，只需重跑 `python docs/testing/gen_test_cases.py --revision <新HEAD>` 重生成三账本并 `--check`，数字会自动刷新；**改完务必先解决 DEF-02（`eol=lf`）再提交**，否则账本会在下次 git 触碰再次 CRLF 污染、CI 再红。

### 基线真值复核（8e7ee65，2026-11-14）

上表已按 HEAD `8e7ee65` 重生成核对（`--check` PASS、CRLF=0）。与本文件早先登记态的差异：

1. **GAP-15 / DEF-03 已闭合**（2026-11-11 用户签字，commit `f4d4575`）：以源码为权威语义；Adjudicator v4 模板 §29/§32 已对齐，一致性测试在位。详见 `COVERAGE_AUDIT.md` GAP-15 行。
2. **团队终审 T-1~T-12 并非全开**：上游 `77951ca` 已回填——T-1/T-3/T-4/T-6/T-7/T-11 DONE，T-8/T-9/T-10 本地 DONE；**仍 OPEN 仅 T-2 / T-5 / T-12**。T-12 已核实确无门禁单测文件；T-2 的 `check_selector_manifest.py` 代码里已有 nodeid 过滤修复，待 owner 复验销项。
3. **「17 卡全 closed / 门禁 PASS」（commit `7163645`）只作用于 ADR-002 远端控制面**，不等于团队终审通过——`full_stack_review_20261024.md` 结论仍为"不通过"，两者不可混用。
4. **GAP-16 仍 OPEN**：root_cause 复核 0/456。
5. STASH-0：原 review-edits stash 已清；当前 stash@{0} = `e741-prod-wip`（内容未核，非本表范围）。
