# CallWarden 缺陷登记表（Handoff Report）

> 生成时间：2026-11-09 ｜ 审计基线 HEAD：`7e178cb`（源）/ 本地提交 `4133187`（含账本对齐）
> 门禁裁决：**红，不能发**。本表列出全部阻断项与已知未知，供分派给其他 agent 修复。
>
> **如何使用**：每条缺陷自包含。①「修复指引」给具体改动点；②「验收标准」给可判定的 done 条件；③「建议路由」给可派发的 agent 域。修复后请在对应条目勾选并回填 PR/commit。

---

## 0. 门禁裁决摘要

| 门禁腿 | 状态 | 说明 |
|---|---|---|
| 静态清单自洽（`gen_test_cases.py --check`） | 🟢 | 三账本与被扫描源码树 `dc6d060a` 字节自洽（2026-11-22 续检 HEAD `dc6d060a`，上游末 12 个输入文件未重生致账本落后，本机备份 + stash + ff pull 后按 HEAD 重生；revision==HEAD、`--check` PASS） |
| 提交树 CI 比对（远端） | 🔴 | 本机已与 origin/master 同步（0/0）、账本 `--check` PASS；🔴 仅指 **CI 侧实跑未复验**（DEF-01 待 owner 以 CI 结果销项） |
| CRLF 稳定性 | 🟢 | DEF-02 已根治：三账本 + 输入作用域 `eol=lf`（a8002cc/5ecf7db）+ `digest()` LF 归一化（554ac20）；本机 6 文件 CRLF=0、`--check` PASS |
| 团队终审（`full_stack_review_20261024.md`） | 🔴 | 结论**不通过**；T-1~T-12 已于 2026-11-14 全部销项（T-12 最后收口：gen_coverage_manifest.py 脚本行为单测 20 用例入库）；T-13（2026-11-16 新登记的同族越权写）已销项 |
| validate_skip 账本对拍（`--self-check`） | 🔴 | 23 pass / **4 fail**（2026-11-15 实测，详见文末复查小节） |

**缺陷分级**：P0（DEF-01~03、T-1~T-9、T-13）、P1（T-10~T-12、GATE-VSKIP）、已知未知（GAP-16、D-MATRIX、DEBT-LEDGER、STASH-0，不分级但不可瞒）。

---

## 1. 缺陷总表

| ID | 标题 | 级别 | 建议路由域 | 状态 |
|---|---|---|---|---|
| DEF-01 | 提交树账本 CI false-fail（远端未同步） | P0 | CI/Infra | 本地已修，待 push |
| DEF-02 | CRLF 根因（`.gitattributes` 缺 `eol=lf`） | P0 | CI/Infra | **DONE**（a8002cc：三账本 eol=lf + renormalize，2026-11-11） |
| DEF-03 | GAP-15 治理语义冲突 | P0 | Governance/Rust | **DONE**（2026-11-11 用户签字：源码为准；Adjudicator v4 模板 §29/§32 已对齐，迁移版 role-protocol.md 原本已对齐，一致性测试 test_adjudicator_returned_handoff_reopens_executor_remediation_atomically 在位） |
| T-1 | KR-C nightly 验收门零证据 | P0 | CI/Acceptance | **DONE**（登记过期：semgrep 已移 extras，KR-C closed，known_red.json 有 nightly 绿+Gate 证据） |
| T-2 | Gate 1/2 汇总行污染（反篡改门恒红） | P0 | CI/Gate | **DONE**（2026-11-14 复验销项：`_load` nodeid 过滤在位，PR 114/114 与 full 127/127 本机 exit 0，删 nodeid 条目 exit 2 拦截有效，test_cicd_gates.py 14 用例全绿） |
| T-3 | known-red 零隔离 | P0 | CI/Quarantine | **DONE**（登记过期：ci.yml 已有 deselect step + quarantine job，KR-A/B/C 全 closed） |
| T-4 | KR-A 83 条 residue | P0 | Security/Authority | **DONE**（登记过期：KR-A closed——audit passed=true/finding_count=0/transition=79/sunset 未过期，migrate.py 已退役，MASKING 守卫 tests/test_srv_019.py:144 在位） |
| T-5 | 写路径无 workspace 根约束（越权写） | P0 | Security | **DONE**（2026-11-14 复验销项：`_is_within_workspace` realpath+commonpath 校验在位，相对穿越与绝对越界均 `status=failed` 未落盘，test_fuzz.py 6/6 绿，详见 §3 T-5） |
| T-6 | bandit 门禁范围外 75 条 | P0 | Security | **DONE**（登记过期：bandit 范围已含 db/ scripts/ cw.py config.py，75 条逐条处置入注释） |
| T-7 | Rust 集成测试只编译不执行 | P0 | Rust/CI | **DONE**（登记过期：rust-unit-test job 已有 cargo test --tests 执行步） |
| T-8 | E2E 结果 advisory 不阻断 | P0 | CI/E2E | DONE 本地（e616a90，2026-11-11：按触发形态阻断；tag run 验证待 CI） |
| T-9 | windows-arm64 产物零 E2E | P0 | CI/E2E | DONE 本地（3e773af：新增 e2e-verify-windows-arm64.yml + cryptography 46.0.3 约束；tag run 验证待 CI） |
| T-10 | Windows Python 全量零执行 | P1 | CI/Python | DONE 本地（33f9e32：test-windows job，windows-latest × py3.14 + junction；3 连绿验收待 CI） |
| T-11 | 真空伪测试 `test_uid_isolation` | P1 | Security/Test | **DONE**（1c2533c：真空双 UID 隔离实现，2026-11-11） |
| T-12 | 门禁脚本零单测 | P1 | CI/Gate | **DONE**（2026-11-14 销项：gen_coverage_manifest.py 脚本行为单测 20 用例入库全绿（9b9a698），回归 25 用例绿，真实脚本 --print-summary exit 0，详见 §3 T-12） |
| T-13 | 同族越权写：export/rule-sync/restore 4 入口无 workspace 根约束 | P0 | Security | **DONE**（2026-11-16 销项：4 入口统一 `_is_within_workspace` 前置校验（ebb1f8e），12 用例负向全部 failed 不落盘 + 正向不回归，Rust 侧无同漏，详见 §3 T-13） |
| GAP-16 | skip 站点 root_cause 0/466 复核（reason 字面量 277 / 表达式 166 / 缺省 23） | 已知未知 | Audit | OPEN |
| GATE-VSKIP | `validate_skip.py --self-check` 23 pass / 4 fail（468≠466 站点集、2 项基线哈希未刷新、`conftest.py` 1 条 file_sha256 漂移） | P1 | CI/Gate | OPEN（2026-11-15 新增登记） |
| D-MATRIX | D 专项矩阵未写 | 已知未知 | Test Design | OPEN |
| DEBT-LEDGER | 技术债账本 4 项漏登 | 已知未知 | Audit | OPEN |
| STASH-0 | 原 review-edits stash 已清；`e741-prod-wip` 为上游另一工作副本观察，本机无此条；本机 2026-11-15 实况为两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`），已文件级备份至 `E:/tmp/local_backup_1115/` | 已知未知 | Repo Hygiene | OPEN（待 owner 决定丢弃或保留） |

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
- **复验销项（2026-11-14，HEAD efad200）**：四条验收全部达成——① `check_selector_manifest.py:71-80` `_load` 仅保留含 `::` 的 nodeid 行（汇总行/计数行/注释/空行天然排除）；② 本机按 CI 同款命令实跑：PR profile 114/114 `exit 0`、full profile 127/127 `exit 0`（2026-10-24 恒红的 115 vs 114 不复现）；③ 反篡改实测：从 manifest 删一条 nodeid → `ok=false`、`unregistered_new=1`、`exit 2`；④ `tests/test_cicd_gates.py` 14 用例全绿（含汇总行污染回归锚 + 删条目/加条目/重复条目负向）。

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
- **复验销项（2026-11-14，HEAD 550bbdd；修复实现见 `2e7b2d6` fix(sec) GAP-SEC-01）**：验收全部达成——① `db/db_edit.py` 新增 `_is_within_workspace`（realpath 后 `commonpath` 包含性比对，Windows 下 `normcase` 统一大小写/分隔符，跨盘符 ValueError 判越界），`propose_edit` 写前校验，越界返回 `status=failed` 并附拒绝原因（i18n key `edit_path_out_of_workspace` 中英在位）；② fuzz 回归锚点两条在位（`tests/test_fuzz.py` 测试 4 相对穿越 `../escape_rel.txt`、测试 5 workspace 外绝对路径，均断言 `failed` + `error` 非空 + 未落盘）；③ 本机实跑：相对穿越 `status=failed`（未落盘）、绝对越界 `status=failed`（未落盘）；`test_fuzz.py` 6/6 绿、`test_p2_features.py` 5/5 绿；`gen_test_cases.py --check` exit 0。

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
- **复验销项（2026-11-14，HEAD 9b9a698）**：描述过期修正——check_ci_gates.py/check_selector_manifest.py 已有直接单测（tests/test_cicd_gates.py 14 用例，T-2 复验确认全绿），残余缺口仅 gen_coverage_manifest.py 脚本行为零单测（test_coverage_manifest.py 只断言 manifest 内容，非脚本行为）。销项达成——① 新增 `tests/test_coverage_manifest_script.py` 20 用例（A-F 六组正负向）：collect_cli_tops 正/重复归类 SystemExit、extract_cli_leaves 解析逻辑（choices 提取/跨行 usage 拼接/中文 `用法:` 前缀/无 choices→[]/缺 usage 与含 -h→RuntimeError，monkeypatch subprocess）、build_cli_entries 单命令降级记 errors 与两次提取不一致 SystemExit、build_mcp_entries 双真相源不一致/重复分类/工具名重复/非法 op_class 四类 SystemExit、main() `--output` 写文件与 `--print-summary` 不落盘、真实 subprocess 冒烟一条顶层命令；② 实跑：新文件 20/20 绿，回归 test_coverage_manifest.py + test_cicd_gates.py 25 用例绿；③ 真实脚本全量 `--print-summary` exit 0（cli_top=86/cli_leaf=197/mcp=244，与事实锚点一致）；④ 账本按 9b9a698 重生（fef6429），`--check` exit 0。

### T-13　同族越权写：4 个 db 写入口无 workspace 根约束　【P0】

- **登记（2026-11-16）**：T-5（GAP-SEC-01，commit 2e7b2d6）只收敛了 `propose_edit`；同一写模式在其他 Mixin 复制粘贴扩散，同族入口漏网。威胁模型：agent 可达面 = `cli/main.py` 工具分发表（~L877-935，镜像 MCP registry）。
- **现象**：4 个 agent 可达工具可向 workspace 外任意路径落盘：
  1. `export_module_graph` 的 `output_file`（cli/main.py:877，**op_class=READ_ONLY**——authority 门自动放行类携带写副作用 = 权限分类绕过，本条最重）。写点 `db/db_query.py:1268` `atomic_write_file(output_file)`，无任何约束。复现：`output_file` 指向 workspace 外 → 落盘成功并返回路径。
  2. `rule_sync_agents_md` 的 `target_path`（cli/main.py:924，PROTECTED_MUTATION）。`db/db_agent_rules.py:914-921` `os.path.isabs` → 绝对路径原样采用；相对路径 join workspace 后不拒 `..`。写点 L999。复现：绝对越界与 `../escape_rel.md` 均 `success=True` 越权写（与 T-5 修复前 `_resolve_abs_path` 逐行同款）。
  3. `rule_insert_agents_md_block`（cli/main.py:925），同款模式，写点 `db/db_agent_rules.py:1106`。
  4. `restore_comment` / `restore_all_comments`（cli/main.py:934-935，PROTECTED_MUTATION）。`db/db_comment.py:79/212` 用 DB 中的 rel_path 直接 `os.path.join(workspace_root, ...)` 无包含性校验，写点 L140/L279。静态证据：需 DB rel_path 投毒可达，属防御纵深缺口。
- **根因**：workspace 包含性校验未提炼为共享约束，各 Mixin 各自 resolve 路径且只做 join/采用，不做包含性判定。
- **修复指引**：复用 `EditSafetyMixin._is_within_workspace`（realpath+commonpath+normcase，CodeGraphDB 已组合该 Mixin，四入口均可直接调用）：① `db_query.export_module_graph` 写前校验，越界 raise ValueError；② `db_agent_rules` 两入口 resolve 后前置校验，越界返回 `success=False`；③ `db_comment` 两函数写前校验，`restore_comment` 返回 `success=False`、`restore_all_comments` 记 errors 跳过；④ `export_module_graph` op_class 语义处置见验收标准 ④。
- **验收标准**：
  1. 4 入口 × 2 形态（`..` 相对穿越 / workspace 外绝对路径）负向用例：一律失败（raise 或 `success=False`）且越界路径不落盘；
  2. 合法路径（workspace 内）行为不回归（export 落盘成功、rule_sync/insert 对 workspace 内 AGENTS.md 正常写入）；
  3. Rust 侧复核：`handle_export_module_graph`（rust_ext/src/daemon/query_compat_handlers.rs:1910）只消费 `format`，不消费 `output_file`、不落盘——Rust native 路径无同漏，结论记录在案；
  4. op_class 语义处置：`export_module_graph` 保持 READ_ONLY 不改（改分类将连锁 MCP registry / route matrix 双真相源与账本），收敛写范围至 workspace 内即消除「READ_ONLY 越权写」的实际后果；残余语义张力（READ_ONLY 类在 workspace 内落盘）作为已知限制在销项行明示。
- **建议路由**：Security agent。
- **销项（2026-11-16，HEAD ebb1f8e）**：四条验收全部达成——
  1. 修复实现：① `db_query.py export_module_graph`（L1266-1277）写前 `_resolve_abs_path` + `_is_within_workspace` 校验，越界 raise ValueError；② `db_agent_rules.py rule_sync_agents_md`（L923-939）与 `rule_insert_agents_md_block`（L1093-1105）resolve 后前置校验，越界返回 `success=False`（复用既有错误形态与 i18n key `edit_path_out_of_workspace`）；③ `db_comment.py restore_comment`（L80-84）越界 `success=False`、`restore_all_comments`（L216-219）越界记 errors 跳过；④ 三处均复用 `EditSafetyMixin._is_within_workspace`（realpath+commonpath+normcase），未新增共享抽象。
  2. 单测：新增 `tests/test_t13_write_containment.py` 12 用例全绿（1.60s）——export 相对穿越/绝对越界（pytest.raises + 越界路径不落盘断言）+ workspace 内正向落盘；rule_sync/insert 各 2 负向（`success=False` + 原文件不触碰/不落盘）+ 1 正向（workspace 内 AGENTS.md 正常写入且人工内容不被破坏）；restore_comment 参数化投毒（相对穿越 + 绝对路径）`success=False` 不落盘；restore_all_comments 投毒行 skipped=1 + errors 记录。回归：test_fuzz.py + test_agent_rules.py + test_cicd_gates.py + test_coverage_manifest_script.py 全量 exit 0。
  3. Rust 侧复核：`handle_export_module_graph`（rust_ext/src/daemon/query_compat_handlers.rs:1910-2014）只消费 `format` 参数，**不消费 `output_file`、不落盘**，返回字符串——Rust native 路径无同漏，Python 直连路径是唯一可达面。
  4. op_class 语义处置：`export_module_graph` 保持 READ_ONLY 不改（改分类将连锁 MCP registry / route matrix 双真相源与账本）；写范围已收敛至 workspace 内，越权后果消除；残余语义张力（READ_ONLY 类在 workspace 内落盘，属显式 `output_file` 参数的导出行为）作为已知限制在此明示。
  5. 账本：按 ebb1f8e 重生（c6b3839），`--check` exit 0；selector manifest 不受影响（冻结范围仅 `tests/acceptance/domains/`，新测试在 `tests/` 根目录）。

---

## 4. 已知未知（不分级，但不可瞒）

| ID | 内容 | 影响 | 建议路由 |
|---|---|---|---|
| GAP-16 | `docs/testing/SKIP_SITES.json` 456 个 skip 站点 `root_cause_reviewed=True = 0`；零人工根因复核 | 最大「已知未知」：skip 是否真必要无人证 | Audit agent |
| D-MATRIX | `TESTING_PLAN.md` §7 的 D 专项矩阵（G/I/J/E/W/S/A/X 域 + 16 语言正/负/恢复/并发）未写 | 专项验收无基线 | Test Design agent |
| DEBT-LEDGER | 技术债账本 4 项漏登（何序5.2，独立于机器账本） | 技术债不可见 | Audit agent |
| STASH-0 | 原 review-edits-2026-10-28（11 文件）已清除；`e741-prod-wip` 为上游另一工作副本观察、本机无此条；本机 2026-11-15 实况为两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`） | 冗余、未来 pop 会与已提交内容冲突 | Repo Hygiene |

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

> 派发提示：其他 agent 修改源码后，只需重跑 `python docs/testing/gen_test_cases.py --revision <新HEAD>` 重生成三账本并 `--check`，数字会自动刷新；DEF-02 已根治（`.gitattributes eol=lf` + `digest()` LF 归一化，commit `554ac20`），任何行尾状态的工作树重生账本哈希一致。

### 基线真值复核（da27ddf，2026-11-15）

上表已按 HEAD `da27ddf` 重生成核对（`--check` PASS、CRLF=0；python 定义 7233 / py 文件 698，因 T-12 新增 20 用例脚本行为单测）。与本文件早先登记态的差异：

1. **GAP-15 / DEF-03 已闭合**（2026-11-11 用户签字，commit `f4d4575`）：以源码为权威语义；Adjudicator v4 模板 §29/§32 已对齐，一致性测试在位。详见 `COVERAGE_AUDIT.md` GAP-15 行。
2. **团队终审 T-1~T-12 并非全开**：上游 `77951ca` 已回填——T-1/T-3/T-4/T-6/T-7/T-11 DONE，T-8/T-9/T-10 本地 DONE；**T-2 已于 2026-11-14（HEAD efad200）复验销项**（nodeid 过滤在位、双 profile exit 0、删条目 exit 2 拦截有效、test_cicd_gates.py 14 用例全绿）；**T-5 已于 2026-11-14（HEAD 550bbdd）复验销项**（修复实现见 2e7b2d6：`_is_within_workspace` 校验在位，相对穿越/绝对越界均 failed 未落盘，test_fuzz.py 6/6 绿）；**T-12 已于 2026-11-14（HEAD 9b9a698）销项**（gen_coverage_manifest.py 脚本行为单测 20 用例入库全绿，账本按 9b9a698 重生）。**结论：T-1~T-12 已全部销项，台账内不再有 OPEN 的 T 项**；但 `full_stack_review_20261024.md` 门禁结论仍「不通过」，须 owner 复审改判——销项 ≠ 门禁翻绿。
3. **「17 卡全 closed / 门禁 PASS」（commit `7163645`）只作用于 ADR-002 远端控制面**，不等于团队终审通过——`full_stack_review_20261024.md` 结论仍为"不通过"，两者不可混用。
4. **GAP-16 仍 OPEN**：root_cause 复核 0/456。
5. STASH-0：原 review-edits stash 已清；`e741-prod-wip` 系上游另一工作副本的观察（迁移报告证据指向另一台 Windows 机器的 wanpi 用户目录），**本机 `git stash list` 无此条**；本机 2026-11-15 实况为两条冲突备份 stash：`stash@{0} = local-1114-conflict-merge`、`stash@{1} = local-1113-ledger-alignment`（已文件级备份至 `E:/tmp/local_backup_1115/`，待 owner 处置）。

---

### 复查补充（2026-11-15，HEAD `da27ddf`）

- **pull 冲突与处理**：上游 `31493f3..da27ddf` 7 提交与本机 5 个未提交改动中的 3 个重叠（DEFECT_HANDOFF / SKIP_SITES / STATIC_AUDIT），`git pull --ff-only` 拒绝覆盖。已先文件级备份至 `E:/tmp/local_backup_1115/` 并 `git stash push`（`stash@{0}`），再快进到 `da27ddf`；按该 HEAD 重生账本，`--check` PASS、revision==HEAD、6 文件 CRLF=0。
- **DEF-02 真收口（重点）**：`554ac20` 把 `gen_test_cases.py` 的 `digest()` 改为 CRLF/CR→LF 归一化后哈希，`validate_skip.py` 磁盘对拍同步归一化，`revision_note` 同步声明 EOL 无关。此前 `5ecf7db` 的 `.gitattributes` 只在 checkout/add 生效、上游 CRLF 工作树存量文件仍使哈希漂移；现在**任何行尾状态的工作树重生账本均得同一哈希**，本机与上游账本不再互相判红。本表 §0「CRLF 稳定性」已由 🔴 改 🟢。
- **真值（da27ddf）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7233** / **698** 文件（T-12 新增 `tests/test_coverage_manifest_script.py` 20 用例，7213→7233）｜rust 属性 2122 / cfg 121｜skip 三机制 456（classic 432 / 131 文件，skip 210 / skipif 222 / importorskip 24）｜reason 267/167/22｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/456（GAP-16 仍 OPEN）**。
- **新增门禁红项 GATE-VSKIP（本表首次登记）**：`python docs/testing/planning/skip/validate_skip.py --self-check` 实测 **23 pass / 4 fail / 0 warn**（exit 1）。四项性质不同，不可一并当缺陷：① `skip_sites_id_set_equal`：reviews 复核台账 **468** 站点 vs `SKIP_SITES.json` **456** 站点，差 12 条——两套账本口径未同步，须 owner 明确权威基准；② `file_sha256_disk_match`：`tests/conftest.py:22:8:pytest.skip` 的 `file_sha256` 与磁盘不符（源码改过但复核结论仍绑旧内容），须重新审查该站点；③ `skip_sites_hash_baseline` / ④ `generator_hash_baseline`：`docs/testing/registration/source_baseline.json` 硬编码哈希未随 `SKIP_SITES.json` 重生与 `gen_test_cases.py`（554ac20）改动刷新，属**基线未刷新**，须 owner 按流程重新签字刷新，不是数据错误。提交信息称「失败集与改动前一致、不在本范围」，但按门禁铁闸原则，红项必须登记而非静默排除。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 **无 pytest、无项目 venv、系统 python3 亦无 pytest**，故 T-12 的 20 用例、回归 25 用例、`gen_coverage_manifest.py --print-summary` 均 **NOT_RUN**；本轮采信上游提交信息（`9b9a698`/`da27ddf`）与入库文件 `tests/test_coverage_manifest_script.py`，未做独立实跑验证。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-12 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 7 提交未改该文档，须 owner 复审改判。另 GAP-16（0/456）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。

### 复查补充（2026-11-16，HEAD `d2b28d8`）

- **本轮范围**：上游 `3b51631..d2b28d8` 合入 9 提交（T-13 安全修复 + 销项、defect_learn 写连接迁移、19 项 transition 路由升格、2 次账本重生、写面迁移收口补记）。本机快进拉取，工作树原为空、本地 0 独有提交，无冲突；两条冲突备份 stash 未动。
- **账本无需重生**：上游 `f53a4a6` 已按被扫描源码树 `3c54b48` 重生三账本；`d2b28d8` 仅改 `docs/design/daemon-rust-migration-ledger.md`（§9.28/9.29 写面迁移结论），不在生成器输入作用域，`3c54b48..d2b28d8` 输入零变更。故 revision `3c54b48` ≠ HEAD `d2b28d8` 属正常，`--check` PASS（tests NOT_RUN），本机未重生账本。
- **真值（d2b28d8 源码树 = 3c54b48）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7245** / **699** 文件（T-13 新增 `tests/test_t13_write_containment.py` 247 行 12 用例，7233→7245；698→699）｜rust 属性 **2124** / cfg 121（daemon 写面迁移 +2）｜skip 三机制 456（classic 432 / 131 文件，skip 210 / skipif 222 / importorskip 24）｜reason 267/167/22｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/456（GAP-16 仍 OPEN）**。
- **T-13 销项证据核证（P0 安全）**：`ebb1f8e` 把 4 个同族越权写 db 入口（export/rule-sync/restore 等）统一加 `EditSafetyMixin._is_within_workspace`（realpath + commonpath + normcase）前置校验；新增 247 行 / 12 用例（4 入口 × 2 形态负向：越界必 failed 且不落盘 + 合法路径正向回归）；Rust 侧 `handle_export_module_graph` 不消费 output_file、不落盘，无同漏。台账 T-13 已 DONE。**不可复跑照旧**：本机无 pytest，12 用例 NOT_RUN，采信提交证据与入库文件。
- **写面迁移收口（设计层，非门禁）**：`daemon-rust-migration-ledger.md` §9.28（写面终态 83 项矩阵核查）/ §9.29（defect_learn 迁主库写连接）声明「写面迁移全部收口」。此为 daemon Rust 迁移账本的设计结论，不替代团队终审门禁结论。
- **GATE-VSKIP 复核（2026-11-16 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1），4 项与 11-15 登记完全一致（`skip_sites_id_set_equal` 468≠456、`file_sha256_disk_match` conftest.py 1 条、2 项 `source_baseline.json` 硬编码哈希未刷新）——本轮 9 提交未触及该项，状态不变。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仍是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 9 提交仍未改该文档，须 owner 复审改判。另 GAP-16（0/456）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。

### 复查补充（2026-11-17，HEAD `4c82a58`）

- **本轮性质**：用户报「代码更新」，实为上游 `d2b28d8..4c82a58` 8 提交已在本地（与 origin 0/0，无待拉取）。工作树仅本机上轮 3 个文档对齐改动（COVERAGE_AUDIT / TESTING_PLAN / DEFECT_HANDOFF），因上游未碰这些文件而完整保留，内容经核**依然正确**。
- **8 提交内容**：`1b42f05` 退役 Rust CLI defect learn 本地写（external.rs −241 行）+ 清理存量测试失败、`2d3cd98` 安全契约与 ADR-002 对齐、`b6c5c21` S4-FU 卡 review 闭环、`5fe2bf6` §38 验收矩阵逐行对账复跑、`2987b33` split 治理用例改跑模块级隔离 daemon（消除测试残留卡）；`8171747`/`f0e26be`/`4c82a58` 三次按新 revision 重生账本。
- **真值零漂移（4c82a58 = 2987b33 源码树）**：MCP 244 / CLI 86·21·233 / python 7245·699 / rust 2124·121 / skip 456（classic 432·131）/ reason 267·167·22 / schema 0 / root_cause 0/456，与上轮完全一致。`--check` PASS、6 文件 CRLF 0、账本 revision `2987b33` ≠ HEAD `4c82a58`（末笔账本重生）属正常绑定。
- **门禁状态（不变）**：`validate_skip.py --self-check` 11-17 实跑**仍 23 pass / 4 fail**（4 项与 11-15 登记完全一致，本轮 SKIP_SITES 仅改 reason 文本未影响项集）；团队终审 `full_stack_review_20261024.md` 与缺陷台账本轮均**未被改**，结论「不通过」不变。T-1~T-13 全销项 ≠ 门禁翻绿，须 owner 复审改判。**门禁裁决：红，不能发**（GAP-16 0/456、GATE-VSKIP、D-MATRIX、DEBT-LEDGER 均未闭）。本机无 pytest，T-13 12 用例 NOT_RUN。

### 复查补充（2026-11-18，HEAD `9d100da`，daemon 优雅停机闭环）

- **本轮范围**：用户报「代码更新」，上游 `4c82a58..9d100da` 8 提交已在本地（与 origin 0/0，无待拉取）。本机工作树上轮 3 份文档对齐改动因上游未碰这些文件而完整保留。
- **8 提交内容**：全部为 daemon 优雅停机主线——`612aeaf` `/health` 证书到期监控 + slow RPC watchdog + stop 优雅退出审计、`1c161cf` accept_loop shutdown token + drain + dedup 崩溃残留清理 + `cw daemon stop` 子命令、`ad32fe0` stop 幂等修复（陈旧 manifest 下返回 not_running）、`11a31bd` `daemon.shutdown` RPC（Windows 优雅停止全平台闭环）；`4a96019`/`d961a84`/`a9efe4a`/`9d100da` 四次按新 revision 重生账本。新增 `tests/test_cert_expiry_monitor.py`、`tests/test_cli_daemon_stop.py`、`tests/test_slow_rpc_watchdog.py`。
- **真值（9d100da = 11a31bd 源码树）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7270** / **702** 文件（7245→7270，+25，三组新测试）｜rust 属性 **2132** / cfg **122**（2124→2132）｜skip 三机制 **461**（pytest.skip 211 + skipif 225 + importorskip 25；classic 436 / 133 文件）｜reason **271/167/23**（互斥口径，合计 461）｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/461（GAP-16 仍 OPEN）**。上述当前态口径已同步至 COVERAGE_AUDIT §1/§3、TESTING_PLAN §8，带日期历史快照原值保留。
- **账本一致性**：上游 `9d100da` 已按被扫描源码树 `11a31bd` 重生三账本（末笔 9d100da 自身即账本重生），revision `11a31bd` ≠ HEAD `9d100da` 属正常绑定；`gen_test_cases.py --revision 11a31bd --check` PASS、6 文件 CRLF=0、schema 漂移 0（`missing_current_tools`/`obsolete_schema_tools` 均空）。COVERAGE_AUDIT / TESTING_PLAN / DEFECT_HANDOFF 当前态口径本轮已按新真值同步。
- **GATE-VSKIP 复核（2026-11-18 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1）——`skip_sites_id_set_equal` ledger 468≠SKIP_SITES 461、`skip_sites_hash_baseline` 与 `generator_hash_baseline` 两处 `source_baseline.json` 硬编码哈希未刷新、`file_sha256_disk_match` `tests/conftest.py:22:8:pytest.skip` 1 条不一致。root_cause 字段 461 条全为 `UNREVIEWED`。本轮新增测试未触及该项，状态不变。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 无 pytest、无项目 venv，新增三组测试及 T-13 12 用例均 **NOT_RUN**，本轮采信上游提交证据与入库文件，未做独立实跑验证。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 8 提交未改该文档，须 owner 复审改判。另 GAP-16（0/461）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`）仍在，未动。

### 复查补充（2026-11-19，HEAD `1a43a99`，PKI 热加载 + write.commit 治理）

- **本轮范围**：用户报「代码更新」，上游 `9d100da..1a43a99` 18 提交已在本地（与 origin 0/0，无待拉取）。本机工作树上轮 3 份文档对齐改动因上游未碰这些文件而完整保留。
- **18 提交内容**：全部为证书/PKI 与写治理主线——`602f06f`/`bd658bf` 证书自动续期 + rotate-ca 双 CA 窗口、`85f05ec` 证书热加载不重启轮换、`3ce9fe5` 外部 CA 导入（CW_CA_CERT/CW_CA_KEY）fail-closed、`2a40898` 默认 principal 兜底改 pr_owner、`2729e4d` 换代 CA subject DN 可区分化、`e5945ae` write.commit 全量 content + new_hash 唯一格式 + daemon sha256 fail-closed、`de6dc8d` 对账裁决收口、`aa844b3` stop 核实进程退出后再报 stopped、`1a43a99` §9.38 探针转正 env-gated pytest 资产；另有 `1a93ee3`/`d9cbf7a`/`cccc5aa`/`8907c2d`/`1a43a99` 五次按新 revision 重生账本。新增 `tests/test_client_cert_auto_renew.py`、`tests/test_http_manifest_discovery.py`、`tests/test_remote_write_governance.py`。
- **真值（1a43a99 = de6dc8d 源码树）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7287** / **703** 文件（7270→7287，+17，三组新测试）｜rust 属性 **2162** / cfg **122**（2132→2162）｜skip 三机制 **462**（pytest.skip 212 + skipif 225 + importorskip 25；classic 437 / 133 文件）｜reason **272/167/23**（互斥口径，合计 462）｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/462（GAP-16 仍 OPEN）**。上述当前态口径已同步至 COVERAGE_AUDIT §1/§3、TESTING_PLAN §8，带日期历史快照原值保留。
- **账本一致性**：上游 `1a43a99` 已按被扫描源码树 `de6dc8d` 重生三账本；末笔 `1a43a99` 自身改了 `tests/test_remote_write_governance.py`（该文件在 de6dc8d 已存在，非新增），其最终态已计入账本 hash，revision `de6dc8d` ≠ HEAD `1a43a99` 属正常绑定。`gen_test_cases.py --revision de6dc8d --check` PASS、6 文件 CRLF=0、schema 漂移 0。
- **GATE-VSKIP 复核（2026-11-19 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1）——`skip_sites_id_set_equal` ledger 468≠SKIP_SITES 462、`skip_sites_hash_baseline` 与 `generator_hash_baseline` 两处 `source_baseline.json` 硬编码哈希未刷新、`file_sha256_disk_match` `tests/conftest.py:22:8:pytest.skip` 1 条不一致。root_cause 字段 462 条全为 `UNREVIEWED`。本轮 18 提交未触及该项，状态不变。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 无 pytest、无项目 venv，新增三组测试及 T-13 12 用例均 **NOT_RUN**，本轮采信上游提交证据与入库文件，未做独立实跑验证。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 18 提交未改该文档，须 owner 复审改判。另 GAP-16（0/462）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`）仍在，未动。

### 复查补充（2026-11-20，HEAD `65d9793`，测试失败清零 §9.39~§9.47）

- **本轮范围**：用户报「代码更新」，上游 `1a43a99..65d9793` 9 提交已在本地（与 origin 0/0，无待拉取）。本机工作树上轮 3 份文档对齐改动因上游未碰这些文件而完整保留。
- **9 提交内容**：全为失败根治/清零主线——`72ce920` C7 残留 cascade/reconciliation 自建隔离 workspace 归一、`0f47d81` principal DB grant 整数 workspace_id 静默丢行修复、`6b83ec4` collab 治理写族 E2E 锁定 + `_handle_collab` 退出码口径修复、`7d3578a` A 族隔离栈逃逸与授权种子口径更正、`236e74a` B 族陈旧断言清零、`b869cb6` C 族字面量断言结构化、`62f1cf9` CLI 面全量回归 A/B/C 清零确认、`ad4f8cf` 4 条 live round-trip 隔离 daemon 化、`65d9793` §9.47 全量分段门禁 + F 簇 38 项归因清零。改 30+ 测试文件（`test_task_close_gate.py` / `test_task_split_steps.py` 等），3 次账本重生。上游只触及两个测试 gate 文件（`test_lease_gate_empirical.py` / `test_task_close_gate.py`），未改任何门禁/终审文档。
- **真值（65d9793 = ad4f8cf 源码树）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7290** / **703** 文件（7287→7290）｜rust 属性 **2163** / cfg **122**（2162→2163）｜skip 三机制 **462**（pytest.skip 212 + skipif 225 + importorskip 25；classic 437 / 133 文件，均不变）｜reason **274/165/23**（互斥口径，合计 462；字面量 +2 / 表达式 −2）｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/462（GAP-16 仍 OPEN）**。上述当前态口径已同步至 COVERAGE_AUDIT §1/§3、TESTING_PLAN §8，带日期历史快照原值保留。
- **账本一致性**：上游 `65d9793` 自引用重生三账本（末笔即账本重生，输入 hash 含所有 tests 最终态），revision 记 `ad4f8cf`（被扫描源码树）≠ HEAD `65d9793` 属正常绑定；`gen_test_cases.py --revision ad4f8cf --check` PASS、6 文件 CRLF=0、schema 漂移 0。留意本轮 §9.47 宣称「F 簇 38 项归因清零」属上游测试源码层修复，**不等价于** GAP-16 的 SKIP_SITES root_cause 人工复核（仍 0/462、全 UNREVIEWED），两者不是同一口径。
- **GATE-VSKIP 复核（2026-11-20 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1）——`skip_sites_id_set_equal` ledger 468≠SKIP_SITES 462、`skip_sites_hash_baseline` 与 `generator_hash_baseline` 两处 `source_baseline.json` 硬编码哈希未刷新、`file_sha256_disk_match` `tests/conftest.py:22:8:pytest.skip` 1 条不一致。本轮 9 提交未触及该项，状态不变。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 无 pytest、无项目 venv。上游虽宣称 A/B/C/F 族失败清零，本机 **NOT_RUN** 未做独立实跑验证，采信上游提交证据与入库文件。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 9 提交未改该文档，须 owner 复审改判。另 GAP-16（0/462）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`）仍在，未动。

### 复查补充（2026-11-21，HEAD `0ec83fb`，CLI rc 收口 + daemon 安全加固；账本落后已重生）

- **本轮范围**：用户报「代码更新」，上游 `65d9793..0ec83fb` 13 提交已在本地（与 origin 0/0，无待拉取）。本机工作树上轮 3 份文档对齐改动因上游未碰这些文件而完整保留。
- **13 提交内容**：CLI 退出码口径收口（`230ff904` §24.5 本地校验 rc=2/daemon 拒绝 rc=1、`a63de7f6` rc 残差、`c788787a` dispatcher 级 False→rc=1、`dfb7c502` stdin/未接线防御）、cli_reference 四段式收口（`98ef3a85`/`6524c886`/`626618e2`）、`e3bc9d1e` §9.49 i18n 动态 key 占位符门禁、`4896a7f2` §9.50 全量门禁复跑、daemon 安全加固（`7f94609e` capability registry fail-closed、`a545fc32` principal 前置链 instance 强制解析、`ad4b4aed` 双轨身份契约、`0ec83fb` HTTPS 三项加固：gz 解压 clamp / PKI 私钥 Windows DACL / renew 自动吊销旧证书）。上游只触及门禁文档外的源码/测试/cli_reference，未改终审文档。
- **账本落后已重生（本轮关键动作）**：上游末笔账本重生停在 `c788787a`（revision `a63de7f`），其后 5 提交改 `cli/main.py` + 6 个 rust daemon 源码却未重生，致用记录 revision `--check` 报 `STATIC_AUDIT.json` 不一致。按惯例先文件级备份三账本至 `/e/tmp/callwarden_ledger_backup_20261121_233058/`，再按 HEAD 完整 SHA `0ec83fb` 重生三账本；重生后 revision==HEAD、`--check` PASS、6 文件 CRLF=0、schema 漂移 0。本轮**有本机账本重生改动**（非仅文档对齐），待 owner 处置是否提交。
- **真值（0ec83fb 源码树）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7313** / **704** 文件（较上轮文档 7290/703 +23/+1，增长在 a63de7f 前已发生、本轮据实对齐）｜rust 属性 **2184** / cfg **122**（2163→2184）｜skip 三机制 **462**（pytest.skip 212 + skipif 225 + importorskip 25；classic 437 / 133 文件，均不变）｜reason **274/165/23**（互斥口径，合计 462，不变）｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/462（GAP-16 仍 OPEN）**。SKIP_SITES / TEST_CASES 本轮无变化。当前态口径已同步至 COVERAGE_AUDIT §1、TESTING_PLAN §8，带日期历史快照原值保留。
- **GATE-VSKIP 复核（2026-11-21 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1）——4 项与上轮完全一致（`skip_sites_id_set_equal` ledger 468≠SKIP 462、`skip_sites_hash_baseline` 与 `generator_hash_baseline` 两处 `source_baseline.json` 硬编码哈希未刷新、`file_sha256_disk_match` `tests/conftest.py:22:8:pytest.skip` 1 条不一致）。本轮 13 提交未触及该项，状态不变。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 无 pytest、无项目 venv，上游 §9.49/§9.50 门禁复跑与 rc 收口测试均 **NOT_RUN**，本轮采信上游提交证据与入库文件，未做独立实跑验证。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 13 提交未改该文档，须 owner 复审改判。另 GAP-16（0/462）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。两条冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`）仍在，未动。

### 复查补充（2026-11-22，HEAD `dc6d060a`，workspace 身份派生 + skills 层；冲突解决后提交）

- **本轮范围与冲突处理**：用户报「代码更新 + 有冲突 + 解决后提交」。诊断：本地落后 origin/master **31 提交**、4 文件未提交（3 文档 + 上轮重生账本），本地 0 独有提交、无 merge 冲突标记。按惯例先文件级备份 4 文件至 `/e/tmp/callwarden_conflict_backup_20261122_202056/`，`git stash push` 保存，`git pull --ff-only` 快进（无冲突）。从 stash 恢复 3 份文档——注意 `git checkout stash -- <file>` 触发 `.gitattributes` 把 LF 转成 CRLF（145/213/305 行），已从 LF 备份回灌修正至 CRLF=0；stash 内旧账本（revision `0ec83fb`）不取用，改按新 HEAD 重生。
- **31 提交内容**：workspace 仓库级身份派生与存量迁移（`dc6d060a` REQ-01..04、`e9135e4b`/`5912f52f`/`bf68a696` R0-R3 设计基线/task-map）、skills 层落地（`1a9e2f76`/`9711442e` requirements-cw / code-review-cw / test-cw / ALM 技能 + `cc37b901` 自检器）、report rc 语义（`f8b85888` opt-in `--fail-on-findings` + rc=3）、`1540638d` discover 缓存自愈、`941de565` §48 盘点与账本补交。上游只触及 `STATIC_AUDIT.json`（与我本地重叠），3 份文档未碰。
- **账本落后已重生（本轮关键动作）**：上游末笔账本重生停在 `941de565`（revision `3866311`），其后 `3866311..HEAD` 有 12 个输入作用域文件真变化（`rust_ext/src/daemon/workspace.rs` + `workspace_migration.rs` 等 workspace 身份主线 + `cli/main.py` + `server/daemon_client.py` + 2 测试文件），用记录 revision `--check` 报三账本全不一致。已按 HEAD 完整 SHA `dc6d060a` 重生三账本；revision==HEAD、`--check` PASS、6 文件 CRLF=0、schema 漂移 0。
- **真值（dc6d060a 源码树）**：MCP 244（rust_native 201 / task_rpc 43）｜CLI 86 顶层 / 21 类 / 233 叶子｜python 定义 **7370** / **706** 文件（7313→7370，+57，workspace 身份主线）｜rust 属性 **2207** / cfg **123**（2184→2207）｜skip 三机制 **466**（pytest.skip 214 + skipif 227 + importorskip 25；classic 441 / 134 文件）｜reason **277/166/23**（互斥口径，合计 466；非字面量+缺省 189）｜schema 漂移 0（GAP-14 闭）｜**root_cause 复核 0/466（GAP-16 仍 OPEN）**。当前态口径已同步至 COVERAGE_AUDIT §1/§3、TESTING_PLAN §8、本文件门禁/GAP 表，带日期历史快照原值保留。
- **GATE-VSKIP 复核（2026-11-22 实跑）**：`validate_skip.py --self-check` **仍 23 pass / 4 fail / 0 warn**（exit 1）——`skip_sites_id_set_equal` ledger 468≠SKIP 466、`skip_sites_hash_baseline` 与 `generator_hash_baseline` 两处 `source_baseline.json` 硬编码哈希未刷新、`file_sha256_disk_match` `tests/conftest.py:22:8:pytest.skip` 1 条不一致。root_cause 466 条全 `UNREVIEWED`。本轮 31 提交未触及该项，状态不变。
- **不可复跑项（诚实标注）**：本机自备 Python 3.13.12 无 pytest、无项目 venv，上游 §45/§48 新增测试均 **NOT_RUN**，本轮采信上游提交证据与入库文件，未做独立实跑验证。
- **门禁裁决（不变）**：**红，不能发**。T-1~T-13 全部销项仅是必要条件；`full_stack_review_20261024.md` 结论仍「不通过」且本轮 31 提交未改该文档，须 owner 复审改判。另 GAP-16（0/466）、D-MATRIX、DEBT-LEDGER、GATE-VSKIP 均未闭。两条早期冲突备份 stash（`local-1114-conflict-merge` / `local-1113-ledger-alignment`）仍在，未动。本轮为对齐提交另建 `local-1122-alignment-before-ff`（已取用文档、遗留待 owner 处置）。
