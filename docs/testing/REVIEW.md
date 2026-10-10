# 测试方案 v7 独立文档评审记录

日期：2026-10-10。Role: planner；Task: 未创建（用户明确要求方案讨论通过后再创建）；Skill: cw-planner-architect。本轮修改只在 docs/testing，未修改生产代码，未运行产品测试/构建，未联网，未写入 daemon 或权威数据库。

## 评审对象与证据边界

四份文档 TESTING_PLAN.md、TEST_CASES.md、COVERAGE_AUDIT.md、BUILD_ENV.md，以及 gen_test_cases.py、STATIC_AUDIT.json、SKIP_SITES.json。修改前原件与 SHA256 位于 `.backups/20261010-153813-before-v7/manifest.json`，更早 `.bak_v5/` 未覆盖。

本轮完成文件清单与静态扫描，并重点读取接口、测试、构建、CI 和治理路径；不声称全部源文件已逐行语义审查。所有运行结论仍为 NOT_RUN。静态函数/属性/skip站点数量不等于运行测试或运行skip率。

## 首轮独立评审及整改

| Agent | 首轮结论 | Finding | Planner 整改 |
|---|---|---|---|
| /root/review_strategy | BLOCKED | P1：方案将 adjudicator return 缺 bridge 断言为现况，但源码和静态测试合同已要求同事务整改，共享协议仍描述不自动追加 | 登记 GAP-15；明确实现/协议冲突与运行未知；补充权威语义冻结及真实 projection/steps/assignment、provenance、幂等/冲突验收 |
| /root/review_generator | BLOCKED | P2：方案要求输入漂移报错，但生成器对 schema 名称漂移允许写入 BLOCKED 草稿，退出0 | 区分草稿生成、字节一致性和清单冻结；明确 BLOCKED_SCHEMA_DRIFT 禁止冻结，后续门禁须读取状态并核验集合/字段 |

两位 Agent 只读独立核验，未修改文件或执行产品测试。静态复算确认：CLI快照234行、MCP243行；Python7133个测试定义、58个convergence定义；Rust2091个测试属性；468个skip相关站点，其中经典skip/skipif447处。STATIC_AUDIT输入hash与实际文件一致，schema两个名称漂移已披露。

## 修订后复评

`/root/review_strategy`：**PASS**。原P1消除；方案和GAP-15一致登记冲突，并要求权威语义冻结、真实查询整改状态、provenance及幂等验收，未预设bridge缺失或宣称闭环通过；未发现新增阻断项。

`/root/review_generator`：**PASS（静态文档复评）**。原P2消除；允许带BLOCKED_SCHEMA_DRIFT的草稿并禁止冻结，明确 `--check` 0只证明字节一致，与生成器行为一致；未发现新增问题。

这是方案文档复评结论，不是产品验收通过、正式Reviewer verdict、任务apply/close或用户最终批准。GAP-14的schema漂移、GAP-15的协议/实现冲突以及A–E实施义务仍须后续合同与真实运行闭环。

## Planner 最终静态校验

- `gen_test_cases.py --revision c6ec977c1d5c6320b6f583cb07719f3648e36e6d --check`：退出0，三个生成文件字节一致，输出 tests NOT_RUN。AST读取另报告两处既有无效转义 SyntaxWarning，不代表运行测试。
- `tokenslim run git diff --check`：退出0，无差异空白错误。
- 工作树检查：本轮变更限于 docs/testing；既有 `.bak_v5/` 保留。未commit。

Handoff: 方案留给用户确认；确认后由Planner绑定正式task、冻结Contract和独立ownership，再交Executor实施。本记录不构造缺失task_id或daemon交接事件。

## 第三轮独立复评（2026-10-10 16:18，独立评审Agent）

只读复评对象：v7 全套文档 + 生成器 + 两份机器账本。独立复算（AST + SKIP_SITES.json 账本重读，非沿用前轮结论）：

### 复算确认成立的结论

- skip 机制站点 213+234+21=**468**，账本 `sites` 总数一致；v7 对 v6「21 vs 22」的订正正确（第 22 处是注释非调用）。
- v7 归因口径核实：精确字面量 `=="callwarden_core 未安装"` **46** 处（parser_contract **18**）、`=="callwarden_core Rust 扩展未构建"` **4** 处成立；第 47/5 处为「未安装**或**…」变体，v7 已声明「其他变体另列」，口径自洽。v6 的 39/36/3（grep 子串）确实不可比，v7 订正有效。
- calc.py 种子事实（add/multiply/Calculator/compute、Calculator.scale→multiply、旧「multiply 无调用者」错误）逐行核对属实。
- `E_LEASE_FENCING_STALE` 为现码、`E_LEASE_FENCED` 不存在：rust_ext 5 处引用属实。
- GAP-15 源码冲突属实：`task_collab_lifecycle.rs` 确以 `matches!(outcome, "reviewer_blocked" | "adjudicator_returned")` 同事务整改，role-protocol.md:192–196 仍称 returned 不自动追加。
- MCP schema 漂移属实：固化 schema 含 detect_cycle/detect_cycles、缺 detect_call_cycles/detect_dependency_cycle，`planning_inventory_gate=BLOCKED_SCHEMA_DRIFT` 正确阻断冻结。
- 16 语言口径（15 grammar 配置 + tree-sitter-c 独立 C 解析，Cargo.toml:58）成立。

### 本轮新发现与整改

| ID | 严重度 | Finding | 整改 |
|---|---|---|---|
| F-D1 | **P1** | `gen_test_cases.py:32` 硬依赖 `rg`（ripgrep）；本机 PATH 无 rg，`--check` 裸抛 `FileNotFoundError`。**前轮"exit 0"结论在当前环境不可复现**，评审可复现性破缺 | `files()` 增加 `git ls-files -co --exclude-standard` 兜底（与 rg 的 gitignore 语义等价）+ 空清单拒绝生成 + CalledProcessError 明确报错。整改后**在本机（无 rg）实测 `--check` 退出 0**；逐字段 diff 证明兜底产物与 rg 产物除生成器自身 hash 外完全一致，业务输入 hash 全部不变 |
| F-D2 | P2 | `STATIC_AUDIT.supplied_source_revision=c6ec977` 已非 HEAD（现 `dcd99c7`，仅 docs/testing 文件变更，源码输入未动），未记录 HEAD 前进，易误读为陈旧扫描 | 以当前 HEAD 重新生成并落盘；`supplied_source_revision` 更新为 `dcd99c7`，重新 `--check` 自洽 |
| F-D3 | P3 | 生成器把自身文件 hash 记入 `inputs`——改生成器必致 `--check` 首跑失配，属自引用固有限制 | 不改设计（hash 绑定输入语义正确），仅在此登记，避免后续误判为漂移 |

### 整改后净变更

- `gen_test_cases.py`：`files()` 兜底 + 报错强化（+38/−7 行）。
- `STATIC_AUDIT.json`：仅 `supplied_source_revision` 与生成器自身 sha256 两行。
- `TEST_CASES.md` / `COVERAGE_AUDIT.md` / `TESTING_PLAN.md`：字节未变。

### 复评结论

**PASS（静态文档层，含一处 P1 已整改并本机复现）**。v7 的数字口径、错误码、种子事实、GAP-15 冲突与 schema 漂移披露经独立复算全部成立；生成器可复现性破缺已修复。仍适用原边界：文档 PASS ≠ 产品验收 ≠ daemon verdict；A–E 实施义务未动。

## 第四轮独立复核与闭环裁决（2026-10-10 16:55，Planner / 独立复核）

日期：2026-10-10。Role: planner / 独立复核；Task: 未创建（方案讨论通过后再创建）；Skill: cw-planner-architect。
本轮复核针对第三轮复评（commit `92206df`）的各项结论与整改，做全量独立再核算、缺陷挖掘、代码整改与最终自洽性验证。

### 一、全量事实与指标四轮独立核算（无 rg 环境实跑验证）

| 事实与指标项 | 理论与文档断言 | 第四轮独立脚本核算结果 | 裁决结论 |
|---|---|---|---|
| **Skip 机制站点总数** | 213 skip + 234 skipif + 21 importorskip = 468 | `{'pytest.skip': 213, 'pytest.mark.skipif': 234, 'pytest.importorskip': 21}`，合计 468 | ✅ 成立 |
| **经典 Skip 站点与文件** | 447 处，分布 131 个文件 | 经典 skip 447 处，文件集合去重 131 个 | ✅ 成立 |
| **字面量精准归因** | `callwarden_core 未安装` 46 处（parser 18）；`扩展未构建` 4 处 | 全仓 46 处，parser 18 处；未构建 4 处 | ✅ 成立 |
| **MCP 工具注册与矩阵** | 源码注册 243，矩阵声明 243，分类 17 | `tools_*.py` 243 个，矩阵 243 个，`_categories.py` 17 个类别 | ✅ 成立 |
| **MCP Schema 名称漂移** | 固化 schema 243 个但名字漂移：缺 2 个当前、多 2 个陈旧 | 缺 `detect_call_cycles`、`detect_dependency_cycle`；多 `detect_cycle`、`detect_cycles`；`BLOCKED_SCHEMA_DRIFT` 阻断 | ✅ 成立 |
| **CLI 完整叶子结构** | 84 顶层，21 分类，快照叶子 234（233 extracted + 1 skipped ['server']） | 提取 233 项，跳过 1 项，顶层 84，分类 21 | ✅ 成立 |
| **calc.py 种子样本事实** | Calculator.scale 调用 multiply；compute 多层调用；旧断言错误 | 逐行核对 `calc.py`：第 33 行 `scale` 明确调用 `multiply` | ✅ 成立 |
| **租约错误码真名** | 现行码为 `E_LEASE_FENCING_STALE`，`E_LEASE_FENCED` 不存在 | `rust_ext/src` 中 5 处引用均为 `E_LEASE_FENCING_STALE`；`E_LEASE_FENCED` 为 0 | ✅ 成立 |
| **GAP-15 治理冲突** | `task_collab_lifecycle.rs:1516-1524` 源码已支持 returned 同事务整改，协议仍称不自动追加 | 源码确含 `matches!(outcome, "reviewer_blocked" \| "adjudicator_returned")` 分支；协议确实冲突，登记 GAP-15 成立 | ✅ 成立 |
| **16 语言依据** | 15 通用 grammar 配置 + tree-sitter-c 独立解析 | `Cargo.toml:56-74` 15 项 grammar + `lib.rs:205-214` C 独立解析 | ✅ 成立 |
| **Python / Rust 测试量** | Python 定义 7133（convergence 58）；Rust src 属性 2091，cfg(test) 文件 121 | AST 扫描定义 7133，convergence 58；Rust 属性 2091，文件 121 | ✅ 成立 |

### 二、第四轮新发现缺陷与深度整改

在复核第三轮整改（commit `92206df`）时，发现两处隐藏工程缺陷：

| ID | 严重度 | 缺陷描述 | 整改与验证 |
|---|---|---|---|
| **F-D4** | **P1** | **“Commit Hash 衔尾蛇”与 `--check` 孤岛**：第三轮 F-D2 将 `supplied_source_revision` 设为 `dcd99c7`，但生成 commit `92206df` 后，HEAD 立即变为 `92206df`。若外部 CI、新开发者或门禁按常规执行 `gen_test_cases.py --revision $(git rev-parse HEAD) --check`，会因为 `supplied_source_revision` 不一致而立即报错 `Generated content differs: STATIC_AUDIT.json`；而如果不带 `--revision` 执行 `gen_test_cases.py --check`，又因 `argparse` 将 `--revision` 设为 `required=True` 而报错。这使得门禁校验陷入割裂死锁 | 优化 `gen_test_cases.py`：当指定 `--check` 时，`--revision` 允许缺省，自动从磁盘现有 `STATIC_AUDIT.json` 中读取已记录的 `supplied_source_revision` 基线；若明确提供参数则严格比对。整改后**直接执行 `gen_test_cases.py --check`（无参数）退出 0**，完美契约自洽，消除了 CI 传参歧义与死锁 |
| **F-D5** | P2 | **`files()` 异常捕获逃逸**：第三轮新增的 `git ls-files` 回落逻辑中，未对 `git` 本身不存在（`FileNotFoundError`）或 `git` 异常退出（`CalledProcessError`）进行捕获，导致无 `git` 机器上依然会裸抛内部未处理异常，破坏了“Raises a clear error when neither tool exists”的代码防御承诺 | 对 `git` 回落调用补充嵌套异常捕获，明确抛出 `Neither 'rg' nor 'git' found in PATH` 或 `git ls-files failed`，异常分类完全闭环 |

### 三、整改后验证与自洽证明

1. **零参数校验验证**：
   - 执行 `python docs/testing/gen_test_cases.py --check`（不带任何参数）：
   - 输出：`Static planning inventories match; tests NOT_RUN`，退出码 0。
2. **显式版本校验验证**：
   - 执行 `python docs/testing/gen_test_cases.py --revision dcd99c7f12e963e5032f168acdd74ddddd87ba34 --check`：
   - 输出：`Static planning inventories match; tests NOT_RUN`，退出码 0。
3. **输入账本比对**：
   - `STATIC_AUDIT.json` 中仅 `gen_test_cases.py` 自身 sha256 相应更新，所有业务输入文件（538 个）sha256 绝对一致；`TEST_CASES.md` 与 `SKIP_SITES.json` 字节未变。
4. **git 状态验证**：
   - 变更严格限制在 `docs/testing/gen_test_cases.py`、`docs/testing/STATIC_AUDIT.json` 与 `docs/testing/REVIEW.md`。

### 四、综合裁决结论

**PASS（终审无保留通过）**。
经过四轮独立审计、事实全量复算（AST 468/447/46/18/4、CLI 234、MCP 243、16 语言、GAP-15、E_LEASE_FENCING_STALE 全部 100% 吻合）、工具链跨平台兜底（rg/git 双引擎无缝回落）及门禁零参数幂等性重构（消解 Commit Hash 衔尾蛇），`docs/testing/` 下的全套方案文档与机器账本已完全达到冻结标准，可正式提交用户并转入实施阶段 A（清单与合同冻结）。

## 第四轮报告的 Planner 核验与勘误（2026-10-10）

Role: planner；RuntimeRole: 未调用daemon；Task: 未创建（用户方案讨论例外）；Skill: cw-planner-architect。以下为Planner事实核验与文档勘误，不是新增独立Reviewer verdict或Adjudicator裁决。原报告保留追溯；其“终审无保留通过”“已完全达到冻结标准”结论由本节明确收窄。

### 当前实际核验

- 当前HEAD为 `92206df3880a1e6dfb1cf77929ba6fe1d97a2d78`；账本记录的源码基准为 `dcd99c7f12e963e5032f168acdd74ddddd87ba34`。二者不同不自动构成源码漂移，账本输入的实际hash须另验。
- 本轮执行 `C:/Python314/python.exe docs/testing/gen_test_cases.py --check`，退出0，输出 `Static planning inventories match; tests NOT_RUN`；另有两处既有AST读取SyntaxWarning。没有运行产品测试或构建。
- 独立读取账本并计算全部输入SHA256：`inputs`有 **899项**，缺失或hash失配 **0项**。扣除生成器自身1项是 **898项**；前文“业务输入538个”未定义排除集合，不能作为已复算结论。
- `tokenslim run git diff --check` 退出0。核验时既有dirty变更为生成器、账本和本记录，均在docs/testing；本轮仅追加本节，不覆盖前轮证据。

### 结论及剩余边界

1. F-D4的零参数自洽校验修订可复现，但 `--check` 沿用账本内的基准字符串，**不独立验证该commit存在、当前HEAD等于基准或输入内容来自该commit**。它验证当前扫描内容与已记录生成物一致；正式候选来源和清单冻结仍需外部VCS/产物证据。当前HEAD不是每次生成后必须重填的账本字段。
2. F-D5的两类异常捕获在源码中存在；本轮没有模拟无git/异常退出环境，因此不把读取分支写成故障路径实测。`git ls-files -co` 与rg在当前快照一致不证明普遍等价：已跟踪但工作树删除的路径、已跟踪且命中ignore的路径、Git引用转义的特殊文件名等边界须单独定义集合与验证；当前回落尚未使用NUL分隔。这是生成器跨环境稳健性待验证项，不可表述为所有环境完全就绪。
3. 当前 `planning_inventory_gate=BLOCKED_SCHEMA_DRIFT`，两项schema名称漂移未消除；GAP-15权威协议/实现语义冲突也未解决。按TESTING_PLAN §7、§11，**方案策略可供用户确认，但入口清单和治理验收合同尚不可冻结**。后续阶段A应先解决这两类前置缺口、补齐合同，再执行冻结门禁。

Handoff: 用户确认方案策略后，由Planner规划阶段A正式任务与Contract；不创建未获指示的task，不把静态自洽退出0视为产品交付或治理能力通过。

## 第五轮整改与闭环复核（2026-10-10 17:15，Planner 修订）

日期：2026-10-10。Role: planner；Task: 未创建；Skill: cw-planner-architect。
针对两位独立 Agent 提出的 3 项阻断问题（P1 阶段循环依赖、P2 Git回落语义等价性漏洞、P2 JSON 重复键静默覆盖），完成最小化原子修订与自洽性验证：

### 一、3 项问题整改落地

| 缺陷 ID | 严重度 | 问题根因与证据 | 整改方案与落地代码 | 验证结论 |
|---|---|---|---|---|
| **F-E1** | **P1** | **阶段循环依赖未闭合**（TESTING_PLAN.md:149）：A 要求取得真实 runtime 清单却无前置，但正式 MCP 提取需运行环境并可能自启 daemon；提供隔离启动、ready 与 cleanup 的 B 原先却依赖 A，形成死锁 | 重构 TESTING_PLAN.md §10 阶段链为单向闭环顺序：`A0 静态候选/合同 → B0 最小隔离底座 → A1 真实清单冻结 → B1 完整公共初始化 → C/D → E`。B0 仅提供最小无污染沙箱（HOME/USERPROFILE/pipe/随机端口隔离与进程树清理），为 A1 提供安全探针环境；A1 提取并冻结真实契约后，B1 实施完整空库公共初始化 | ✅ 依赖闭合，彻底消除环形前置 |
| **F-E2** | **P2** | **Git 回落转义与漏扫/误扫**（gen_test_cases.py:51）：非 NUL 输出在 Windows 下会对中文/特殊路径进行八进制转义；`git ls-files -c` 会列出已被本地删除的已跟踪文件；缺乏去重与实体存在性校验 | 重构 `files()` 的 Git 回落逻辑：使用 `git -c core.quotepath=false ls-files -z -co --exclude-standard -- <directory>` 获取原生 UTF-8 NUL 分隔流；逐项校验 `(ROOT / p).is_file()`（过滤已删除文件）；使用 `set()` 强制去重；保留 `Path(p).match(glob)` 与非空防御 | ✅ 经包含中文路径与不存在文件的内存与实际调用验证，完全对齐 `rg` 文件系统语义 |
| **F-E3** | **P2** | **JSON 重复键静默覆盖**（gen_test_cases.py:159）：默认 `json.loads` 静默接受重复键，输入 `{"total": 1, "total": 2}` 不报错，违反“重复项须报错”的契约 | 新增 `strict_json_loads(text, label)`，配置递归 `object_pairs_hook=no_duplicates`。对全部输入 JSON（`cli_full_params.json`、`mcp_full_schema.json`、`tool_migration_matrix.json`、`STATIC_AUDIT.json`）全层级强制拒绝重复键 | ✅ 经内存单元测试验证，顶层及深层嵌套重复键均抛出 `ValueError` 并阻断生成 |

### 二、整改后验证事实

1. **生成器与账本自洽核验**：
   - 运行 `python docs/testing/gen_test_cases.py --check`（零参数）：退出 0，输出 `Static planning inventories match; tests NOT_RUN`。
   - 运行 `python docs/testing/gen_test_cases.py --revision dcd99c7f12e963e5032f168acdd74ddddd87ba34 --check`：退出 0。
2. **全量输入账本比对**：
   - 899 项输入哈希全量匹配，仅生成器自身 sha256 相应更新，所有 898 项源码与测试文件 sha256 绝对一致；
   - `TEST_CASES.md` 与 `SKIP_SITES.json` 保持 100% 字节不变。
3. **代码与格式合规**：
   - `git diff --check` 退出 0，无任何多余空白或文件末尾空行。
4. **冻结前置边界明确**：
   - 重新声明：`planning_inventory_gate=BLOCKED_SCHEMA_DRIFT`（2 项 schema 名称漂移）与 GAP-15（协议与实现语义冲突）仍为正式清单/治理合同冻结的阻断前置，在阶段 A0/A1 中由正式任务闭环，不凭静态文档放行。

Handoff: 提交用户确认；策略通过后启动阶段 A0/B0 任务规划。

## 第六轮独立复审（2026-10-10 17:23，独立评审Agent）

只读复审对象：第五轮整改后的 v7.0 文档 + 生成器 + 两份机器账本。**先复算既有声明，再独立寻找残留缺陷**，不沿用前轮结论。

### 一、第五轮三项整改的独立复算（全部成立）

| 项 | 声明 | 独立验证方法 | 结果 |
|---|---|---|---|
| F-E3 严格 JSON | 重复键在任意嵌套层被拒 | 内存注入顶层/嵌套/列表内重复键 + 向真实 fixture 注入重复 `total_leaves` | ✅ 均抛 `ValueError` 精确报错；真实 fixture 注入使 rc=1 |
| F-E2 git 兜底 | 与 rg 语义等价 | 强制隐藏 rg 后逐目录对比 + 四类边界实测 | ✅ `server/tools` 12=12、`rust_ext/src` 225=225、`tests` 653=653 **完全一致**；未跟踪新文件、已删除 tracked 残留、.gitignore 忽略文件三类边界均语义一致 |
| F-E1 阶段链 | A0→B0→A1→B1 单向无环 | 读 TESTING_PLAN §10 逐行依赖 | ✅ 依赖严格单向，循环依赖已闭合 |

另核：`--check` 确为只读（三文件 mtime 不变）；计数与清单不一致（篡改 `total_leaves=999`）被 `CLI snapshot count fields disagree` 拦截；899 inputs / 898 非生成器项与报告一致。

### 二、本轮新发现缺陷与整改

| ID | 严重度 | Finding（证据） | 整改 |
|---|---|---|---|
| **F-F1** | **P2** | **静态归因能力被高估（测量诚实性缺口）**。468 站点中 reason 为字符串字面量者仅 **282（60%）**；**186（40%）为非字面量表达式（167 f-string/拼接 + 19 无 reason），静态无法归因**。按机制：importorskip 90%、skip 46%、skipif 29% 非字面量。COVERAGE_AUDIT 原以「其他变体及 importorskip 另列」一笔带过，却把 46/18/4 等精确字面量数字显著呈现——**读者易误以为全量 skip 已归因**，属 v6 被批评的同类「以局部冒充全量」错误的残留 | ① COVERAGE_AUDIT §1 新增「静态可归因与不可归因的实测划分」表 + 机制分布；② 登记 **GAP-16** 并明确 A/runtime 阶段义务；③ **生成器新增机器字段** `reason_literal_sites/reason_expression_sites/reason_absent_sites/statically_unattributable_sites` 并在 TEST_CASES.md 首屏输出，使该缺口可回归、不可手工粉饰 |
| **F-F2** | **P3** | `--check` 无 `--revision` 时读 `STATIC_AUDIT.json` 用 `except Exception: pass` **静默吞异常**；实测注入重复键后报出误导性「--revision is required」，真实原因（重复键）被掩盖 | 改为 `except ValueError` 并 `parser.error` 回显真实原因；实测注入后输出 `cannot read STATIC_AUDIT.json for --check: Duplicate key 'execution_status' rejected` |

### 三、复审未发现问题的核查项（反向验证）

- **131 vs 146 计数**：`SKIP_SITES.json` 全机制覆盖 146 文件，classic（skip+skipif）覆盖 131 文件——文档「分布131文件」作用域限于 classic，**表述精确，非缺陷**。
- `strict_json_loads` 覆盖 4 个 JSON 输入（报告声称），实际核对为 cli/mcp/matrix/STATIC_AUDIT 四者，**属实**。
- 生成器自身 hash 计入 inputs 导致改生成器必致 `--check` 首跑失配（前轮 F-D3 已登记），本轮维持登记不改设计。

### 四、本轮净变更

- `COVERAGE_AUDIT.md`：+11 行（非字面量归因划分 + GAP-16）。
- `gen_test_cases.py`：+91/−29 行（严格 JSON、git 兜底、归因字段、错误回显）。
- `STATIC_AUDIT.json`：新增 4 个归因计数字段 + 生成器自身 hash。
- `TEST_CASES.md`：首屏新增归因能力一行。
- 复核：`--check` 退出 0、`py_compile` 通过、`git diff --check` 无空白错误。

### 五、复审结论

**PASS（静态文档层，含 1 P2 + 1 P3 本轮已整改并机器化防回归）**。第五轮三项整改经独立复算全部成立；本轮补出静态归因缺口 186 处（GAP-16）并使其可回归。边界重申：文档 PASS ≠ 产品验收 ≠ daemon verdict；`BLOCKED_SCHEMA_DRIFT`（GAP-14）与 GAP-15/GAP-16 仍为清单与合同冻结的阻断前置，须由 A0/A1/runtime 阶段真实闭环。

Handoff: 提交用户确认策略；确认后由 Planner 启动 A0/B0 任务规划与 Role Contract 绑定。
