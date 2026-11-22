# test-cw 技能设计文档（立项稿 v0.4）

> 状态：**立项稿，未实现**。三问已拍板（新建独立技能 / 首版本地直跑 / 沿用 `evidence/<AC-id>.md`）。
> v0.1 主理人初稿 → **v0.2 并入 qa-engineer（凌无缺）测试验收结论**（三处按裁决收紧，§13）→ **v0.3 三口径拍板完成（§12）** → **v0.4 按 backlog-46 真实试跑校准 6 处（§14）**。
> 本稿只定义「跑测试并回填证据」这一个动作，批准后再写 `SKILL.md` 与 `references/`。
> 关联：`requirements-cw`（上游：ac-map / symbol-anchor / R5 收口）、`code-review-cw`（平行：审缺口，不跑测试）、
> `_shared/cw-gates.md`（共享门禁 G0-G6）。

## 1. 定位

**验收执行者**：把 `ac-map.md` 里的一条 AC，变成一份可审计的证据。

一句话：**它不写测试、不修代码，只回答"这条 AC 到底跑过没有、跑出来是什么"。**

`skills/README.md` §1 矩阵里「测试执行」是唯一的 ❌；本技能填的就是这一格。

## 2. 不作（越界即本轮作废）

| # | 越界行为 | 正确归属 |
|---|---|---|
| B1 | 写/改测试代码（含"既有测试写错了顺手改"） | 产 `GAP` 缺陷单 + 建议用例（前置/步骤/预期/建议文件），落码交 executor |
| B2 | 修产品代码、做到修复的根因分析 | 只定位"红在哪、属哪个 AC/symbol/nodeid"；根因与修复归 executor / bug-fix 流程 |
| B3 | 把红跑绿：retry / `--reruns` / 改顺序 / 临时 `--deselect` / `\|\| true` / 降断言 / 删用例 | 唯一合法降级 = 登记 known_red，且受 §5 三条件约束 |
| B4 | 放宽门禁（改 manifest、`known_red.json`、skip 阈值、`pyproject` addopts） | 发现门禁本身错 → 出「门禁缺陷」单交团队决策 |
| B5 | 写未发生的结论、外推、伪造证据 | 未执行的 AC 写 `BLOCKED` + 原因；不触发 CI / 不推代码 / 不建 PR（首版） |

## 3. 五步流程

```
① 定位 AC（读 ac-map 快照）→ ② 选测试集（三源反查 + 强制并入 + 冒烟兜底）
→ ③ 执行（本地，逐批 junit）→ ④ 判据（六条件）→ ⑤ 回填 evidence/<AC-id>.md ＋ GAP 建单
```

**严格串行**：一条 AC 走完五步再走下一条。**先跑后补证据必失真**，视为违规。

## 4. 测试范围选择

输入：`scope.AC[]`（ac-map 快照）、`symbol-anchor` 的 `new|extend` 符号清单、`changed_files[]`、`test-plan.md`。
输出：`select_set = {AC-id → [nodeid…]}` + `gaps[]`。

### 4.1 三源反查建索引

| 源 | 依据 | 做法 |
|---|---|---|
| A（AC→用例） | `test-plan.md` 每行已引用 AC id | 取该行登记的文件/类 → `pytest --collect-only` 展开成 nodeid |
| B（符号→用例） | `new\|extend` 符号清单 | **三类字面量并集**扫 `tests/`：① 符号名（snake）② CLI 参数名（**kebab**，如 `--fail-on-findings`）③ i18n key。三者皆空才允许判 `GAP`；只扫符号名会全空并造出假缺陷（§14-F1） |
| C（改动文件→用例） | `changed_files[]` | 同名测试作**种子**，再取反向 import 闭包——但闭包**只作候选池**，不得直接当执行集（实测 140 文件 / 1307 nodeid，超批上限）；包名以 `PYTHONPATH` 布局为准（本仓是 `callwarden.cli.main`）。见 §14-F2 |

> 本仓回归大量是 wiring / 契约型（`test_*_wiring.py`、`test_abi_contract.py`），**同名假设漏检率最高**；`rust_ext/` 在 Python 侧没有同名对应。

### 4.2 强制并入（改动触及即必须加，不受 AC 映射左右）

- MCP 工具注册 / RPC schema / daemon 边界 / `pyproject` / `cicd/*` 改动 → 并入 `test_abi_contract.py`、相关 `test_*_wiring.py`、以及 `check_selector_manifest.py` 对应 domain manifest 的全量 nodeid；
- `rust_ext/` 改动 → 追加独立 `cargo test` 批次 + Python 侧 `test_*_rust*` / `test_*_daemon*`。

### 4.3 冒烟兜底

任何 scope 都并入**冻结冒烟集** `references/smoke.txt`——**一次冻结，单次任务只读不写**（已拍板，§12-1）。改冒烟集需单独变更单 + 用户确认，禁止在本轮任务内为变绿而增删。

### 4.4 映射三态，禁止静默 N/A

| 态 | 条件 | 处置 |
|---|---|---|
| `MAPPED` | 有 nodeid | 进 `required_nodeids(AC)` |
| `GAP` | 可自动化但无 nodeid | 记 **P1** 缺陷，**该 AC 本轮判 FAIL**（不是 N/A）——GAP 不得退化为变相 N/A |
| `N/A` | **仅当** `test-plan.md` 在**执行前**已标 `level=manual` 且写明人工验收方式 | 记 N/A + 引用该标记 |

- **事后补记 N/A 视为违规**；`GAP(symbol)`（符号无任何测试引用）单独记一条，同样计入本轮结论。

### 4.5 禁令与批次上限

- **检索工具约束**：目录级检索必须限路径 + 限类型（`tests/*.py`，不递归 `tests/`），或走 cw `file_grep`；本仓 `grep -r tests/` 会因 11815 个 py 被 SIGTERM，而**检索失败与「无命中」不可区分**——会直接造出假 `GAP`（§14-F5）。
- 不许只跑 `pytest tests/` 全量：本仓 `tests/` 下 **633 个 `test_*.py`**（另有 11k+ 夹具/语料 py，不计入收集），本地全量跑不完 → 必然演变成"跑一半贴绿"；且全量绿**不产生 AC→nodeid 归属**（evidence 要求 nodeid 级），无关测试全绿对本次 AC 是零信息。
- 不许只跑同名测试（见 §4.1 理由）。
- 单批 nodeid > **800** 或预计 > **10 min** → 拆批；**逐批一个 junit + 逐批 rc/统计写进 evidence**；不允许"只跑一部分就判 PASS"。

## 5. 通过判据（六条件同时成立）

```
PASS(AC) :=
  ① run_revision == `git rev-parse HEAD`                 # 执行时 HEAD，防旧证据冒充本轮
     且 code_revision 存在，且 `git diff --stat <code_revision>..HEAD -- <changed_files>` 为空
                                                            # 追溯验收时 HEAD 已前进不算失效；代码真漂移才 FAIL（§14-F4）
  ② required_nodeids(AC) ⊆ junit 结果 且全部 == passed   # 缺一个 = FAIL
  ③ 该批 failures == 0 且 errors == 0 且 collection_errors == 0
  ④ required 集上 skipped == 0 且 xfail == 0            # 对齐 cicd/check_ci_gates.py:228-237
  ⑤ deselected ∩ required_nodeids(AC) == ∅
  ⑥ command_rc == 0 且 collected > 0 且 collected == len(required)
```

`rc=0` **必要不充分**：`-k` 全过滤、误用 `--collect-only`、空收集都可能 rc=0，所以 ⑥ 必须再校 `collected`。

### 5.1 known-red 的唯一合法姿势（三条件缺一即 masking）

1. 条目在 `docs/testing/known_red/known_red.json`，`status=open` 且未过期；
2. **不在本轮任何 AC 的 required 集内**（即 §5 第 ⑤ 条）；
3. evidence 显式列出被 deselect 的 nodeid + 登记 id + 到期日。

`known_red.py` 到期检查 `exit 2` → 关联 AC 判 FAIL（理由写「known-red 过期」）。**静默 deselect 视为伪造证据。**

### 5.2 明令禁止的"假绿"（结果反推）

先有结论再挑子集/挑命令；临时 `--deselect` 未登记红例；`--reruns`/retry 跑绿只贴最后一次；`|| true` 吞 rc；只贴 `-q` 计数行不贴 summary 行；复制上一 revision 的 rc 与统计。

## 6. 证据 schema（`evidence/<AC-id>.md`，八段缺一即未通过）

| # | 段 | 内容 |
|---|---|---|
| 1 | 头部 | AC-id / REQ-id / 标题 / task_id / `revision`(git HEAD) / branch / 时间（带时区） |
| 2 | 环境指纹 | OS+arch；`C:/Python314/python.exe -V`；`pytest --version`；`rustc -V` + `cargo -V`；`CW_TEST_MODE=1`；`PYTHONPATH=C:/git_work`；`CARGO_TARGET_DIR` 实际值；依赖来源 |
| 3 | 命令原文 | 可复制执行的完整命令行（含 `--junitxml` 路径、nodeid 列表或 `-k` 表达式、cwd）；**禁止「跑了相关测试」式概述** |
| 4 | 统计 | collected / passed / failed / error / skipped / xfail / deselected + `rc` |
| 5 | **必需 nodeid 完成表** | 一行一个 `nodeid → outcome`，**全列，不许只列通过的**——判据 ② 的唯一可核对载体 |
| 6 | 关键输出摘录 | 失败 traceback 首尾、断言消息、关键日志行，≤20 行，脱敏 |
| 7 | 判读结论 | `PASS\|FAIL\|BLOCKED` + 一句话理由 + **指向判据哪一条**（如「卡在 ④ skipped=2」） |
| 8 | 附件指针 | `reports/junit-<AC>.xml` 相对路径（+sha256 可选） |

**禁止写入**：token / 密钥 / 连接串；真实用户名与绝对私有路径；全量日志或整文件 diff；未执行的计划语句（"预计/将验证/待补充"）；他人 evidence；推测性结论；把 skip/xfail 写成 pass；用历史运行代替本 revision。

**缺失即失败**：`ac-map.md` 的「证据位置」为空或指向不存在文件 = 该 AC 未通过。

**生成方式（§14-F6）**：证据骨架必须由 **junit 解析脚本生成**（nodeid 完成表、统计、命令原文），人工只写「判读」与结论；**禁止手写 nodeid 表**（149 行手抄必错，漏一行即判据 ② 失效）。junit 原件随证据一起落盘。

## 7. 反 happy-path 六检查（执行阶段可验证）

| # | 检查 | 判据 |
|---|---|---|
| C1 | **advisory**：每个 AC 的 required 集含 ≥1 条**负向** nodeid | nodeid 匹配 `invalid\|illegal\|error\|fail\|denied\|reject\|expired\|empty\|overflow\|missing\|bad_`，或 AST 扫该测试函数体内含 `pytest.raises` / `assertRaises`；无负向且 `test-plan.md` 未标「该 AC 无异常分支 + 理由」→ **提示**（不作 FAIL 依据；实测按名匹配会漏掉 `assert code == 3` 形态，见 §14-F3） |
| C2 | **advisory**：负向断言强度 | `pytest.raises` 应带具体异常类型且后续断言错误码/消息；出现 `pytest.raises(Exception)`、裸 raises、`except Exception: pass` → 提示弱断言。静态判据有假阴性（helper 返回 code 再 assert 的写法 grep 不到），**不作 FAIL 依据** |
| C3 | 边界成对 | 涉及数值/长度/时间/并发上限的 AC，required 集内参数化 id 至少覆盖 `min-1 / min / max / max+1` 中的 **2 个点**，且 evidence 第 6 段给出对应断言值 |
| C4 | 门禁类 AC（`cicd/*`） | 必须有「坏输入 → 非零退出码」用例，evidence 记录期望退出码（0/2/64 三态）；只测 rc=0 的 happy path → FAIL |
| C5 | skip 反查 | required 涉及文件里 `skip`/`xfail`/`skipif` 标记数 > 0 时，逐个列出条件与理由，否则 FAIL |
| C6 | mutation-lite 探针 | 对本轮 ≥1 个 `new\|extend` 关键分支手工注入反向条件（**改完必 revert**，revert 后记录 `git status` 干净），确认至少 1 条 required 用例变红；**没变红 → 测试无效，该 AC 判 FAIL**。探针内容与 revert 证据入 evidence，禁止把注入留在提交里 |

C6 的强制范围（已拍板，§12-3）：**`must` 故事强制；`should` / `could` 可选**。理由：它是唯一能证明测试真会红的手段，但每条都做会被耗时逼到绕过——绕过比不做更差。

## 8. 与上游的接口

| 方向 | 契约 |
|---|---|
| `requirements-cw` R5 收口第 6 步 | 改调本技能（或同会话内按本流程执行）；`ac-map.md` 的「证据位置」由本技能填写 |
| 覆盖缺口 | 建 `action:"test"` 单，首行带 `REQ-xx`（无对应需求记 `REQ-GAP`），格式遵 `_shared/cw-gates.md` §G6 |
| 测试失败 | 建 `action:"fix"` 单，`description` 贴断言差异原文，不贴全量日志 |
| `code-review-cw` | 不跑测试；它审「测试缺口」，本技能负责「跑并留证」 |

## 9. 首版边界（明确不做）

- 不触发 CI、不轮询远程结果（本地直跑，命令可复现即可；CI 结果只能作附加参考，不能替代本地本 revision 证据）；
- 不设覆盖率门槛（只把数字记进证据，不判绿——避免重演「阈值噪音逼人 pin select」）；
- 不做性能 / 安全专项测试；
- Rust 与 Python nodeid 形态不同，首版共用一套 schema，靠「命令原文」自解释。

## 10. 交付结构（批准后实现）

```
skills/test-cw/
├── SKILL.md                      # 五步流程 + 判据 + 边界（做薄）
├── references/evidence-spec.md   # 八段 schema + 六检查 + 失败示例
├── references/smoke.txt          # 冻结冒烟集（只读，见 §12-1）
├── install.ps1                   # 三家宿主安装 + 注入 _shared/cw-gates.md
├── README.md
└── DESIGN.md
```

`_shared/cw-gates.md` 的 G6 已覆盖建单格式；若需新增门禁（如「证据完整性」），加在 `_shared/`，不加在本包。

## 11. 自验收标准（这个技能怎么算做完）

1. 拿 `requirements-cw` 的一个真实 feature 跑通：`ac-map.md` 每条 AC 都有 `evidence/*.md` 或（执行前已标 `level=manual` 的）`N/A`；
2. 至少 1 条 AC 判 FAIL → 成功建单且首行带 REQ，`task_id` 回写证据第 1 段；
3. 证据里的命令可原样复制重跑，结果一致；
4. known-red 排除被如实记录（nodeid + 登记 id + 到期日），非静默；
5. 全程无 retry 刷绿、无改断言、无 xfail 掩盖、无事后补 N/A。

## 12. 三口径（已拍板）

| # | 口径 | 决定 | 影响落点 |
|---|---|---|---|
| 1 | 冻结冒烟集由谁定、能不能改 | **一次性冻结在 `references/smoke.txt`，单次任务只读**；改动须单独变更单 + 用户确认，禁止本轮为变绿而增删 | §4.3 |
| 2 | `GAP` 缺陷默认级别 | **P1 阻断**：可自动化但无测试 → 该 AC 本轮判 FAIL + 建 P1 单。理由：否则 GAP 退化成变相 N/A，缺口被洗白 | §4.4 |
| 3 | C6 mutation-lite 强制范围 | **`must` 强制、`should`/`could` 可选**。理由：唯一能证明测试会红的手段，但全量强制会被耗时逼到绕过，绕过比不做更差 | §7 C6 |

## 13. 修订与裁决记录（v0.1 → v0.2 裁决；v0.3 仅固化 §12 三口径）

| # | 变化 | 裁决理由 |
|---|---|---|
| 1 | **N/A 收紧**：v0.1 允许「无可执行断言 → 记 N/A」；v0.2 改为**仅执行前 `test-plan.md` 标 `level=manual`**，事后补记违规 | 事后 N/A 是洗白通道：跑不出来就自称"人工项"，追溯链立刻失效 |
| 2 | **known-red 收紧**：v0.1 只要求「不计入通过 + 如实记录」；v0.2 加 **`deselected ∩ required = ∅`** | 原稿有漏洞：required 用例被 deselect 仍可能判 PASS，等于假绿 |
| 3 | **测试集选择升级**：v0.1 的 L1-L3 → **三源反查 + 反向 import 闭包 + 强制并入清单** | 本仓回归以 wiring/契约为主（633 个 `test_*.py`，含 `test_abi_contract.py`、`test_b*_wiring.py`），同名假设漏检率最高；`rust_ext` 无 Python 同名对应 |
| 4 | **判据 3 条 → 6 条**（加 revision 新鲜度、collected 校验、deselected 交集） | `rc=0` 可被 `-k` 全过滤与空收集伪造；旧版本无新鲜度校验，历史证据可冒充本轮 |
| 5 | **新增 C6 mutation-lite** | 唯一能证明测试真会红的手段；成本高故列为范围待拍板，而非无脑全量强制 |

**未采纳/折中**：无。qa-engineer 的六组结论全部并入；仅把 C6 的适用范围从「未限定」收为「待拍板」，避免立项阶段就锁死成本。

## 14. 试跑校准（v0.3 → v0.4，靶点 backlog-46）

在 `f8b8588`（§46 报告型命令 rc 语义）上按本稿真跑一遍：**149 例全绿，6 条自动化 AC PASS，1 条 manual AC 判 N/A → 需求未关闭**。
完整记录见 `.design/test-cw-dryrun-20261122/CALIBRATION.md`，6 处差异如下：

| # | 立项稿原文 | 实测问题 | 已改为 |
|---|---|---|---|
| F1 | 源 B = AST 扫 import / 属性引用 / monkeypatch / MCP 工具名 | 真实入口是 argv 里的 **kebab 字面量**；只扫符号名 `fail_on_findings` → **0 命中**，会被误判成 `GAP`（假缺陷） | 三类字面量并集：snake 符号名 + kebab CLI 参数名 + i18n key（§4.1） |
| F2 | 源 C = 反向 import 闭包迭代到不动点 | 闭包实测 **140 文件 / 1307 nodeid**，超 800/批上限；且包名是 `callwarden.cli`（依赖 PYTHONPATH），按 `cli.main` 查 0 命中 | 闭包仅作候选池；执行集 = A ∪ B ∪ 强制并入；升格须 ≤800/批拆（§4.1） |
| F3 | C1/C2 作为 FAIL 判据 | 断言形态是 helper 返回 code 再 `assert code == 3`，按 `excinfo.value.code` grep → 0 命中。**但 mutation 探针 8/8 变红**——静态检查假阴性，C6 才是硬证据 | C1/C2 降为 advisory；C6 升为 `must` 硬判据（§7） |
| F4 | ① `evidence.revision == HEAD` | 追溯验收时 HEAD 已前进（`c10a1e5` vs `f8b8588`），按字面会误杀；实际 `git diff --stat` 为空 | 拆 `run_revision` + `code_revision` + 相关文件零漂移（§5） |
| F5 | （缺）检索工具约束 | `grep -r tests/` 被 SIGTERM（11815 py），`rg` 不在 PATH；**检索失败与无命中不可区分** | 限路径 + 限类型，或走 cw `file_grep`；检索失败须显式报错（§4.5） |
| F6 | （缺）证据生成方式 | 手抄 149 行 nodeid 不可行，漏一行判据 ② 即失效 | junit 脚本生成骨架 + 人工判读；junit 随证据落盘（§6） |

**试跑顺带验证成立的部分**（不改）：判据 ③④⑤⑥ 均可机器判定；`N/A` 的合法形态真实存在且确实导致需求未关闭；强制并入清单有效（`i18n/*.json` 与 `docs/cli_reference.md` 若不并入，两条 AC 会静默缺席）；149 例单批 < 2 min，本地直跑路线成立。
