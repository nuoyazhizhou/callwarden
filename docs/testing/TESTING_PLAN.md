# CallWarden 测试方案（v7 · 真实仓库语义验收与交付门禁）

> 日期：2026-10-10。状态：**方案评审稿，测试尚未实施，产品尚未验收**。
> 本轮用户授权Planner修改文档/文档生成器并委派独立Agent评审；方案通过后再创建task。
> 本轮不改生产代码、不跑测试、不联网、不调用daemon写面，不构成正式task Handoff。
> 配套：TEST_CASES.md、COVERAGE_AUDIT.md、BUILD_ENV.md、STATIC_AUDIT.json、SKIP_SITES.json、REVIEW.md。

## 1. 目标与事实边界

对发布范围内全部CLI命令和MCP工具，在全新隔离环境及冻结真实仓库上证明业务正确性，证据完整后才能判断候选产物达到交付标准。

本轮完成全仓可见文件清单与主源码/文档静态扫描，重点读取入口、路由、治理契约和测试链路；**不声称逐行审读完全部项目，不声称测试通过**。静态事实、历史运行、当前运行和计划要求分别标识。机器扫描范围、算法和输入hash见STATIC_AUDIT.json。

权威：现行AGENTS.md、`.agents/skills/cw-task-loop/references/role-protocol.md`及其现行amendment、接口契约和实现。README/历史设计发生冲突时登记差异，不直接当作现行行为。capability是否启用须在未来隔离测试实例查询，不从文档推算。

非目标：本轮不实现测试基建、不修产品、不改变发布能力、不制造task/identity/lease。文档评审不是daemon verdict。

## 2. 完整覆盖的定义

单位是“发布配置 × 公开入口 × 业务场景”。发布配置冻结OS/架构、入口形态、transport/security profile、依赖和启用capability；不得通过静默缩减配置达标。

| 维度 | 每入口适用义务 | 通过证据 |
|---|---|---|
| 正常业务 | 有效前置下至少一条正常场景；声明不可用单列 | 独立oracle与业务结果一致 |
| 参数 | 全部positionals/options/schema字段；默认、缺失、类型、enum、边界、互斥 | 每个适用等价类有selector和预期 |
| 负向 | 无效对象、状态、权限、路径、超额输入 | 指定错误码/结构、允许审计事件；无非法业务改变 |
| 写入 | 每种承诺效果、幂等、冲突、回滚/取消 | 前后对象、文件、图谱和事件精确断言 |
| 恢复 | 适用的超时、断连、重启、半完成重试 | 失败可解释、状态一致、无旁路成功 |
| 跨入口 | CLI/MCP共同业务 | 先独立oracle校验，再比较关键语义 |

“全参数覆盖”不代表穷举无限输入或所有组合。每个参数有测试映射，危险组合显式列举，其余按冻结pairwise/等价类规则处理。

入口覆盖率=有完整且通过的适用语义合同的入口数/发布配置入口数；参数覆盖率=有通过映射的适用参数义务数/全部适用参数义务数；场景覆盖率=通过必需场景数/冻结必需场景数。三者分别报告，不能以函数数或调用次数替代。

旧runner的EXPECTED_BUSINESS只是探测分类。正常case遇not_found/缺lease必须失败；负向case仅精确匹配预期才通过；未知结果记UNKNOWN并阻断。合法空结果允许，不能一概要求payload非空。

## 3. 用例合同与oracle

case必含稳定ID、入口/profile、requirement/source、风险、前置、仓库revision、真实实体provenance、参数等价类、Act、oracle、允许事件/禁止副作用、timeout、cleanup、selector、实现状态及证据引用。

TEST_CASES是**静态入口/参数规划清单**。目前未完成逐入口case合同和运行证据映射，统一标“待合同化/未计入语义覆盖”；不据此断言已有探测不存在或通过。生成清单不等于可执行测试。

golden来自人工核验样本和冻结契约，不从被测search/stats结果复制预期。API返回ID/hash可定位实体，但业务语义仍由独立oracle验。差分比较不能用同daemon同错误证明正确。

现有calc.py有add、multiply、Calculator及方法、compute；Calculator.scale调用multiply，多处调用add。旧“multiply无调用者”“calc.py恰两函数”错误，须按实际解析契约冻结符号集/调用边/行范围和方向；不能猜测返回字段固定为result["callers"]。

## 4. 仓库与语言矩阵

| 层 | 用途 | 冻结依据 |
|---|---|---|
| R0确定性小仓库 | 图谱、边界、改删、历史、恢复精确断言 | 人工golden；旧种子先纠错 |
| R1真实仓库 | 全量CLI/MCP适用业务 | 首先CallWarden自身固定commit，预选符号与历史独立核验 |
| R2代表性真实仓库 | 多语言、构建上下文、跨仓依赖 | 已可取得的离线快照；实施前冻结名单 |
| R3大型真实仓库 | 吞吐、资源、增量、长期稳定性 | 固定快照/硬件/基线；不能以合成数据冒充 |

corpus manifest必含来源/授权、commit、文件摘要、语言、规模、依赖、适用case、oracle来源、复制方式。原仓只读，所有改动在隔离副本，不复制生产业务数据库。每入口选适合的真实场景，不要求每入口在每仓重复；R0不能替代R1/R2。

16语言依据是15种通用Rust配置加独立C解析，不是JS/TS再次分列。每语言覆盖合法定义/调用或引用、残缺语法、该语言允许Unicode、跨文件与增量改删。HCL按引用契约验收，非法标识符用负向场景。

## 5. 隔离与全新初始化

统一EphemeralDaemonFixture生命周期底座，保留HTTP/UDS/Named Pipe/bridge专用安全适配。RouteStub是adapter单元测试，不强制转为真实daemon。

隔离HOME/USERPROFILE、data root、task/registry/codegraph/toolchain库、CAS、manifest、socket/pipe及客户端环境。端口由daemon bind 0返回实际endpoint，不先探测后绑定。

从空目录使用产品正式初始化/迁移；公共接口取得workspace instance、注册identity、绑定合同、创建真实task/step、lease、snapshot/evidence。禁止固定假ID/hash、客户端derive authority、借生产lease。

产品E2E禁止SQL补表/补workspace/补合同/binding。现有conftest的_ensure_task_db_workspace和_ensure_codegraph_seed_tables可能掩盖初始化缺口，只能作为历史底层测试支持，不能算全新安装/公共链路通过。正式接口无法建立前置时登记产品能力缺口，Planner另冻最小生产修复scope，fixture不能补绿。

ready gate核验source revision、构建hash、PID/executable、manifest/health一致，workspace/snapshot可读且oracle通过。二进制缺失、publish失败、authority不符为必需E2E的ERROR，不skip、不吞异常回退。

日志持续排空，超时保留现场；结束核验进程树、端口/锁和临时资源清理。Job Object/进程组按平台实测，不承诺未验证的100%回收。客户端显式绑定本次实例，禁止生产发现/生产自启动。runner中止也须有清理策略；正常退出与强杀分别记录。

## 6. CLI与MCP真实链路

CLI分别验cw.py源码、安装Python console entrypoint、冻结发布产物；原生Rust CLI如发布，按独立命令树/profile验，不能套Python84顶层。公开面包括子命令、standalone、仍承诺的legacy flag/alias、help/version、格式/i18n/global options。

CLI固化233叶子加server只是起点。重新提取argparse需记录default/choices/nargs/互斥/必填/类型/global继承；现有takes_value快照不足以证明argv正确。server/watch/daemon验启动→ready→真实交互→停止，timeout不是成功。install/setup使用隔离venv/HOME和离线依赖；cw test仅调用受控小模块，防递归验收。help/version按契约验无daemon可用和无业务写入。

每case验rc、stdout/stderr、语义、前后状态和cleanup；正常场景缺参/usage不能算通过。T3强制隔离endpoint/manifest，refresh成功和snapshot就绪是硬前置。

MCP主验收：真实ClientSession→initialize/协商→tools/list（分页）→tools/call→server子进程→daemon→响应。全量适用语义case走公开承诺的transport，不用私有manager或进程内call_tool代表wire覆盖。

验runtime工具/schema集合、required/default/类型、未知工具/无效参数、取消/超时、server/daemon故障、isError/content/structuredContent与业务错误一致。按实际SDK返回契约处理，不可解析文本/未知tuple不能PASS；stdout仅协议，日志stderr。进程内call_tool保留adapter层，独立记账；startup自唤起必须隔离。

跨端只规范化声明的ID/时间等非确定字段，不能忽略业务差异。

## 7. 专项矩阵

| 专项 | 验收义务 |
|---|---|
| G治理 | 合同/identity/binding、独立性、fresh evidence；PASS后Adjudicator有效lease收尾；BLOCKED追加provenance整改，历史不覆盖 |
| G-C能力 | 未声明Planner/decision能力验明确拒绝；声明后才增正向合同；adjudicator return的实现/协议冲突须先解决并冻结验收合同，不预设bridge缺失或闭环已通过 |
| G-L租约 | acquire/renew/release、过期/孤儿、counter单调、旧token拒写；错误码取catalog，现有码为E_LEASE_FENCING_STALE，不编造E_LEASE_FENCED |
| I隔离 | A/B各自完整预期集合，非空预期返回空必须失败；同名不串扰；真实不同OS用户另测 |
| J异步 | submit→running→terminal→结果落地、取消/失败/重复request/参数冲突/重启；job_id返回不等于完成 |
| E编辑 | 内容hash/scope/patch/audit/图谱/回滚；陈旧hash、越界路径、并发冲突拒写 |
| W增量 | 新增/改删/重命名/branch checkout→watcher→snapshot；dirty overlay隔离、同名歧义 |
| S存储 | CAS四阶段崩溃、pending refs、GC/refresh竞态、stale generation、WAL恢复、迁移幂等；对齐cas-gc-protocol |
| A安全 | traversal/symlink/owner/ACL/限额/审计、实际声明auth profile；不能要求当前未认证profile满足未来token能力 |
| X外部 | Git/Semgrep/LSP/向量可用时真实功能、缺失时承诺拒绝/降级；离线规则/模型/依赖，stub不算真实功能 |
| P性能 | 固定硬件/真实corpus，cold/warm、end-to-end/storage分报；延迟上限/吞吐下限/RSS/构建/增量与退化预算 |

父子task按现行权威合同及测试实例核验；不沿用README自动级联关闭宣传。覆盖子任务closed等父门禁、父级条款未满足和越权apply/close。实现/现行协议冲突要阻断登记，不选宽松语义补绿。

已发现治理语义冲突：`rust_ext/src/daemon/task_collab_lifecycle.rs:1516–1524` 对 `reviewer_blocked` 与 `adjudicator_returned` 都准备同事务 provenance-bound `fix_defect`；`task_collab_tests_core.rs:885–920` 的静态测试合同要求整改 step、Executor assignment、`in_progress` 和幂等重放，而 `.agents/skills/cw-task-loop/references/role-protocol.md:192–196` 仍描述 returned 不自动追加 step/reopen。此处只证明源码与协议不一致，未运行证明候选产物实际行为；登记 GAP-15，治理合同冻结前必须明确权威语义并解决冲突。

真实 handoff 后重新查询 projection、steps 与 assignments，核验源 PASS verdict、source step、returned finding 与整改绑定的 provenance；同 request 重放必须幂等，异 request 冲突按冻结合同拒绝。若仍为 READY/ADJUDICATE，记录运行能力缺口；若已有整改，按独立 oracle 验合法绑定、状态和可领取分派。任何一条路径都不能借协议/实现冲突放行。

LLM选择另设非确定性产品实验，记录模型/配置/样本/成本/阈值。默认核心交付不依赖外网/key；若产品承诺该能力则单列必需profile，缺key不能宣称通过。

## 8. 整改门禁与交付门禁

整改期逐入口冻结缺陷指纹（场景/码/签名/版本/owner/修复验收），禁止新增未知缺陷，修复不能抵消新增。历史18项不是当前基线，须新运行复核；临时xfail strict且有到期条件。

交付期发布范围内阻塞缺陷为0。声明不可用须公开冻结契约、明确拒绝case，单列UNAVAILABLE；不能把承诺可用改标签缩减范围，产品范围变化须明确批准。

| 门禁 | 要求 |
|---|---|
| 清单 | source/runtime/schema/测试义务集合一致，无重复/遗漏/未审漂移 |
| 语义 | 入口/参数/必需场景适用义务100%有通过证据，无只调用不断言 |
| 结果 | 必需suite FAIL/ERROR/UNKNOWN=0；必需适用case SKIP/XFAIL=0；collection/中止阻断 |
| 平台 | 不适用有冻结依据；特性在适用且承诺target通过，不以平台skip逃避全部验证 |
| 生命周期 | 全新安装/升级/重启/承诺恢复通过，无SQL补偿、无生产依赖 |
| 产物 | 同候选release干净安装及真实入口通过，不以源码debug替代 |
| 性能 | 冻结硬件/corpus/profile预算通过；未校准不宣称达到SLO |
| 证据 | 完整/同版本/新鲜；缺报告/selector、空suite不放行 |

运行skip率仅辅助，用实际适用collected nodeid和运行结果计算；collection模块skip另列。静态447/7133不能称执行skip率。平台分母事先冻结，零unexpected skip硬门禁不能被总skip≤5%抵消。

Python coverage只说明Python层，Rust需独立模块/分支义务。2091为src属性数，非cargo执行数，也非全部integration/bin测试。不得把--lib daemon::称全量Rust。覆盖预算按关键handler/负分支冻结，不以无基线60%总行覆盖代替功能完备。

## 9. CI与命令（待实施）

构建配置/候选命令见BUILD_ENV。Windows显式Python3.14；本机TokenSlim上下文误检测Node，正式实施先修正探测/核对命令，不用npm替代Python/Rust验收。CI是否部署TokenSlim在profile明确，本轮不执行命令模板。

PR跑清单/文档一致性、适用Python/Rust快速测试、真实隔离协议及关键写/负面。nightly跑全仓库/平台、恢复/并发/外部/性能。release汇总同候选hash全部必需profile；复用nightly须revision/依赖/产物一致且证据新鲜。

源码suite、daemon E2E、artifact E2E分job且前置明确。Rust覆盖适用src/integration/bin，singleton敏感先隔离/串行分组，不用临时skip吞义务。全量pytest和convergence不能重复计同case。

check_ci_gates/check_skip_rate当前不存在，仅登记待实现职责。gate需完整结果与expected selector集合，拒绝空/缺/重复/截断/过期/版本不符/collection error。JUnit递归处理testsuites子节点；业务case报告核验入口覆盖，不能只数pytest函数。

## 10. 复杂度、ownership与顺序

跨基建、两客户端、治理/存储、CI与产物，多个独立验收目标，不是atomic_hotfix。本次文档修订为同一讨论线程；未来按独立ownership/不交叠白名单建任务，不机械按每分类/phase建child。

| 阶段 | 交付与验收 | 路径上限（实施前逐文件冻结） | 依赖 |
|---|---|---|---|
| A清单/合同 | runtime清单、case/参数映射、逐站点skip账本、profile/corpus；无混口径 | docs/testing、独立manifest/提取脚本 | 无 |
| B基建 | 空库公共接口初始化、隔离/ready/cleanup，无SQL补绿/生产发现 | 专用tests harness与自验收 | A |
| C接口 | 全量CLI/MCP wire语义、写/常驻/参数/跨端，逐case结果 | 独立CLI/MCP测试目录与fixture；共享文件串行 | B |
| D专项 | G/I/J/E/W/S/A/X和16语言，正负/恢复/并发 | 按domain冻结测试目录/fixture | B、相关C |
| E交付 | 安装产物、平台/性能、报告硬门禁 | workflows、release验收和独立gate脚本 | C、D |

各合同排除未冻结生产路径和用户权威库。产品前置缺口另冻最小daemon/client修复scope，产品修复后重验公开链路，不能扩充fixture补绿。task号/基线/schema/命令/rollback/角色合同方案通过后绑定，本文不虚构。

回滚：文档生成失配停生成并保留旧证据；已标记schema漂移的规划草稿可留存，但阻断清单冻结；连接非隔离实例、清理越界、实体无provenance立即阻断；产物升级失败保留旧runtime/业务库，禁止删WAL/SHM。具体实施合同冻结并验证rollback。

## 11. 证据与独立评审

每run记录run_id、revision/dirty摘要、依赖版本、artifact hash、corpus/profile/capability、完整selectors、CLI argv/rc/stdout/stderr或MCP request/response、oracle/前后状态、JUnit/coverage、daemon日志、耗时和cleanup。相关变更发生则旧证据失效；raw token只在进程内，证据脱敏。

TESTING_PLAN维护策略；TEST_CASES生成静态入口/参数和待合同化状态；COVERAGE_AUDIT解释事实/缺口；BUILD_ENV分离源码配置与历史环境；STATIC_AUDIT/SKIP_SITES用于复算，不代表运行通过。

v6原件与hash保留在`.backups/20261010-153813-before-v7/manifest.json`，更早.bak_v5保留。生成器禁止硬编码通过/历史缺陷/skip率；重复项、计数或源/矩阵/分类集合失配须报错。schema与源的名称漂移允许生成显式带 `planning_inventory_gate=BLOCKED_SCHEMA_DRIFT` 的规划草稿，禁止冻结。当前 `--check` 退出0仅表示生成字节一致，不能解除该阻断；后续清单冻结门禁必须读取该字段并要求schema集合及逐字段核验通过。

独立Agent只读PASS/BLOCKED审查本方案，Task未创建按用户讨论例外，禁止修改/测试/网络/daemon写。Planner归档原结论及finding到REVIEW.md，整改后复评；文档PASS不等于产品PASS或真实注册Reviewer verdict。
