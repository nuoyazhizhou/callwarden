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
