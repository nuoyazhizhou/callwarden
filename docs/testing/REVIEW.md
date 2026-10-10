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
