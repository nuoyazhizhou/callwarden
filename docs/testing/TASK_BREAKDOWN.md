# CLI/MCP真实仓库验收任务拆分草案（v1，离线审计稿）

日期：2026-10-10。Role: planner；RuntimeRole: 未调用daemon；Task: 未创建；Skill: cw-planner-architect。

用户授权只编写拆分草案，**不入库、不派工、不运行产品测试或构建**。本文TP编号是文档引用键，不是task_id。正式task、workspace instance、identity、lease、Role Contract均未绑定；本文不是可直接领取的派工合同，也不伪造planner capability。正式实施须重新核验daemon能力并通过受支持路径绑定，缺口由Planner规划修复，不直连SQL。

## 1. 基线、目标及复杂度

代码HEAD参考 `db952964ca49e07d77c033e886298a3e004542af`；方案基线是当前修订工作树中的TESTING_PLAN/COVERAGE_AUDIT/BUILD_ENV/生成器与账本，**不是仅HEAD内的旧版本**。已有7个tracked文档/生成器变更及备份untracked不归未来Executor自动capture。审计通过后先冻结逐文件hash与dirty归属，再创建正式合同。

目标：发布范围内CLI/MCP的入口、参数、正常/负向/写入/恢复/跨端语义完整通过真实仓库验收，并有可复算交付门禁。当前243工具、234快照叶子只是候选集合；按profile取得真实入口后冻结分母。原生Rust CLI、standalone、legacy flags/alias和global options单独建入口义务，不能消失于叶子计数。

复杂度高：存在独立的资产定义、初始化基建、两个客户端、治理及其他domain、平台产物与CI ownership，不是atomic_hotfix。相同ownership内部步骤留在同一任务；下表独立任务各有独立交付与验收。阶段不等于child。未来可归同一规划父任务；产品修复另有独立scope，普通失败整改留同任务fix_defect，不新建remediation child。

## 2. 状态与共享合同

所有卡均为DRAFT。TP-01/02/03/04是首批候选合同；路径可审计但实际profile、corpus、命令和证据版本尚待它们产出，故不称execution_ready。下游卡为条件合同，依赖完成后Planner补齐精确case/nodeid/命令/路径再冻结，不授权Executor自行扩大scope。

共同excluded：所有未列allowed的源码编辑路径；生产业务库/manifest/lease/CAS；生产源码（TP-05只含明确治理文档）；archive、备份、其他Agent dirty文件。各任务都可只读源码。以下新路径尚不存在，表示拟建产物，不是现有实现。每卡只编辑自己的allowed源码/文档，不修改别卡、本文或现有五份核心方案；需要同步核心方案由Planner另作文档修订。

源码编辑白名单与运行写面分别冻结。每个执行卡另有唯一run临时根：正式合同指定其绝对路径、批准父目录、owner标记及run_id，解析后必须在批准的临时父目录内，且不与生产/其他run/源仓路径相交。仅在此根内允许该卡运行生成venv、隔离源码副本、target/build/cache、corpus副本、DB/CAS/manifest/socket/报告；各子目录用途写入合同。守护进程cwd及HOME/USERPROFILE/data/toolchain/cache全部指向该run授权根，不能借默认路径写共享源仓或全局缓存。cleanup只清理有本run归属证据且仍位于批准父目录内的路径。TP-06输出只复制必要hash绑定候选到自己的证据目录，消费方从只读候选复制到自己的run根。

共同正向：声明的expected obligation集合逐项通过且有独立oracle。共同负向：缺前置/空套件/清单缺漏/UNKNOWN/非法副作用/越界连接/证据陈旧一律阻断；严格负例不等于正常功能通过。无daemon、artifact或fixture不能skip必需suite。

共同证据：`deliverables/testing/<TP编号>/<run_id>/`（仅对应卡可写），含输入/输出hash、revision与dirty归属、candidate/profile/corpus、case/nodeid、argv或MCP请求响应、rc、前后状态/oracle、日志、耗时与cleanup；脱敏。此目录只用于未来证据，不在本轮创建。任务验收冻结其证据manifest，后续不覆盖历史run。

共同回滚：只撤回该卡已归属的commit/白名单文件；不能reset共享工作树、删旧证据、修改生产数据。运行停止只清理已证明属于本run的进程和临时根，强杀/清理失败保留证据并阻断。代码基线/候选hash变化必须重新判断证据有效性。

共同Handoff：Executor→独立Reviewer→独立Adjudicator，真实task_id及身份从daemon取得；本稿不填写占位身份。正式PASS不等于已apply/close。Planner不得实施产品修复或冒充正式verdict。

## 3. 依赖与任务索引

`TP-01/02/03/04 → 资产与语义前置`；`TP-01+02 → TP-06 → TP-07 → TP-08C/08M → TP-08F → TP-09 → TP-10C/10M及TP-11各专项 → TP-12/13/14 → TP-15`。

治理额外链 `TP-04 → TP-05 → TP-09、TP-11G`；skip审计 `TP-03 → TP-10/11报告 → TP-13`。TP-08F只冻结入口/schema，不替代治理合同对齐或全量运行证据。TP-13门禁实现可在报告schema冻结后提前开发，但最终验收必须等待真实输入。下表“相关C”须在domain合同中列精确依赖，不能默认为等待所有客户端全量完成。

| 编号 / 阶段 | ownership与独立交付 | 前置 | 拟写路径（除此以外禁止写） |
|---|---|---|---|
| TP-01 / A0 | 清单/义务格式及静态候选 | 方案策略确认 | docs/testing/planning/inventory/ |
| TP-02 / A0 | 发布profile、离线corpus和oracle资产 | 方案策略确认 | docs/testing/planning/assets/ |
| TP-03 / A0 | skip逐站点根因/适用性审计合同 | 方案策略确认 | docs/testing/planning/skip/ |
| TP-04 / A0 | GAP-15权威语义差异及对齐设计 | 方案策略确认 | docs/testing/planning/governance/ |
| TP-05 / G | 已批准治理权威文档对齐 | TP-04设计批准 | AGENTS.md（仅returned整改语义相关段，禁止改TokenSlim自动块）；.agents/skills/cw-task-loop/references/role-protocol.md；docs/design/cw-role-handoff-task-loop-v2-amendment.md |
| TP-06 / B0前置 | 可追溯测试候选构建/离线依赖 | TP-01、02 | tests/acceptance/artifacts/ |
| TP-07 / B0 | 最小安全进程/传输底座 | TP-06；TP-01/02合同 | tests/acceptance/harness/bootstrap/ |
| TP-08C / A1 | CLI真实树/参数提取 | TP-07 | tests/acceptance/inventory/cli/；docs/testing/manifests/cli/ |
| TP-08M / A1 | MCP wire集合/schema提取 | TP-07 | tests/acceptance/inventory/mcp/；docs/testing/manifests/mcp/ |
| TP-08F / A1 | 清单/义务合并冻结 | TP-08C、08M、01、02 | docs/testing/manifests/frozen/ |
| TP-09 / B1 | 全新公共初始化与实体前置 | TP-08F、05、07 | tests/acceptance/harness/initialization/ |
| TP-10C / C | CLI全入口语义/参数合同 | TP-09、08F | tests/acceptance/cli/ |
| TP-10M / C | MCP全工具wire语义/参数合同 | TP-09、08F | tests/acceptance/mcp/ |
| TP-11G / D | 治理/租约全生命周期 | TP-09、05及相关C | tests/acceptance/domains/governance/ |
| TP-11I / D | workspace/用户/权限隔离 | TP-09及相关C | tests/acceptance/domains/isolation/ |
| TP-11J / D | 异步任务/取消/重试 | TP-09及相关C | tests/acceptance/domains/jobs/ |
| TP-11E / D | 编辑、hash、并发冲突与回滚 | TP-09及相关C | tests/acceptance/domains/editing/ |
| TP-11W / D | watcher/dirty overlay/增量 | TP-09及相关C | tests/acceptance/domains/incremental/ |
| TP-11S / D | CAS/GC/WAL/迁移与崩溃恢复 | TP-09及相关C | tests/acceptance/domains/storage/ |
| TP-11L / D | 16语言解析/引用独立golden | TP-09、02及相关C | tests/acceptance/domains/languages/ |
| TP-11X / D | Git/Semgrep/LSP/向量等外部依赖 | TP-09、02及相关C | tests/acceptance/domains/external/ |
| TP-12 / E | 发布产物安装/升级/平台验收 | TP-10C/M、全部适用TP-11 | tests/acceptance/release/ |
| TP-13 / E | 结果汇总/覆盖/skip/来源硬门禁 | TP-01报告合同，最终验收等待10/11/12/14 | scripts/testing_gates/；tests/acceptance/gates/ |
| TP-14 / E | 性能/资源/长期稳定性 | TP-02、09、相关10/11 | tests/acceptance/performance/ |
| TP-15 / E | CI接入与同候选发布证据闭环 | TP-12、13、14及适用功能证据 | .github/workflows/ci.yml；.github/workflows/testing-acceptance.yml（拟新增） |

原生Rust CLI若纳入发布，TP-08C/10C必须按独立命令树分卡，不能把Python CLI ownership无限扩展。TP-02先确定实际发布矩阵：现有平台workflow是候选证据，不自动等于本次全部承诺target。LLM若在承诺发布范围内，按TP-11X独立拆实验合同与成本/模型门禁；缺key不能标通过，不承诺则有明确scope依据。

## 4. 首批四张合同卡

### TP-01 静态清单与case/报告合同

单一owner：inventory-contract；不修改产品/旧runner/现有生成器。五步：①绑定源/矩阵/分类/快照hash；②生成静态候选入口与缺漏（含独立flag入口）；③定义case/参数义务、selector、report JSON schema；④实现严格验证器及错误样本；⑤独立审计报告。

allowed逐文件（所有basename均以 `docs/testing/planning/inventory/` 为根）：`candidate_entries.json`、`case_contract.schema.json`、`run_report.schema.json`、`validate.py`、`validation_examples.json`、`CONTRACT.md`。动态default/schema类型未实测标待核，不捏造真实runtime。

验收命令（未来创建文件后冻结）：`C:/Python314/python.exe docs/testing/planning/inventory/validate.py --self-check`，以及同脚本 `--candidate candidate_entries.json`（CONTRACT定义cwd/绝对解析规则）。正向合法多profile义务、跨端映射与collection报告；负向重复键/ID、缺入口、孤立selector、未知状态、非法N/A、分母未冻结、schema漂移错误放行全部非零。self-check不触发产品。

成功：静态source/matrix/category集合一致，当前schema漂移明确BLOCKED，候选数可复算；每种公开入口有责任归属，参数义务不丢失。报告格式能表示setup/call/teardown、collection、UNKNOWN、NOT_TRIGGERED和各层结果。Handoff给TP-02/03/06与后续消费方；不得在此冻结runtime清单。

### TP-02 profile、离线真实仓库与独立oracle资产

单一owner：acceptance-assets。五步：①确定候选发布矩阵及产品决策点；②盘点R0/R1/R2/R3离线可得资产；③固定commit/文件摘要/依赖/授权；④人工golden与性能预算制定规则；⑤校验复制和manifest完整性。

allowed逐文件（所有basename均以 `docs/testing/planning/assets/` 为根）：`profiles.json`、`corpora.json`、`oracle_catalog.json`、`budgets.json`、`validate_assets.py`、`ASSET_CONTRACT.md`。实际corpus只允许复制到合同明列的run临时根；不在源仓写入，不入库生产数据。R2/R3尚无冻结名单，不能填假路径或以合成数据替代；不足由Planner规划离线资产准备/产品范围决策。

验收：`C:/Python314/python.exe docs/testing/planning/assets/validate_assets.py --self-check`；`--verify-manifests`核验可得离线资产hash/版本及manifest。真实符号/调用/引用oracle必须独立人工可复核，calc.scale→multiply等纠错固化。负向缺仓库、错commit/hash、未知语言、偷换平台、缺外部依赖资产、未批准N/A均非零。

成功：每个拟承诺profile/corpus义务都有明确版本与可取得证据；性能预算包含校准环境、测量次数/统计方式和判定规则，未校准不写SLO已达标。缺业务选择标DECISION_REQUIRED，缺技术资产规划内部修复；该卡未完成时TP-06不冒然构建或联网下载。

### TP-03 skip逐站点审查与运行映射合同

单一owner：skip-audit。四步：①以SKIP_SITES及source hash建审查清单；②逐站点判断前置/平台/缺陷/历史机制；③冻结nodeid/collection与运行关联规则；④实现验证及人工审查报告。

allowed逐文件（所有basename均以 `docs/testing/planning/skip/` 为根）：`site_reviews.json`、`runtime_mapping.schema.json`、`validate_skip.py`、`AUDIT_CONTRACT.md`。不改已有tests中的skip；缺陷解除在对应实现任务内发生，不能把447站点当运行skip。

验收：`C:/Python314/python.exe docs/testing/planning/skip/validate_skip.py --self-check`、`--verify-reviews`。全468站点唯一且hash匹配；source变更要求重新审查，非单纯沿用行号。282/167/19只语法，importorskip模块前置单列。每条人工分类有源码理由与owner，未完成为UNREVIEWED阻断审查完成宣称。负向遗漏/重复、以reason字面量冒充根因、未触发捏造运行reason、collection虚构nodeid、删除必需suite逃逸全部拒绝。

成功：静态根因审查的完成标准及实际结果明确；不要求此阶段运行全部站点。后续TP-10/11输出真实运行关联，TP-13核验必需skip=0及NOT_TRIGGERED合法依据。

### TP-04 GAP-15语义差异与对齐设计

单一owner：governance-contract。四步：①比较现行AGENTS/protocol/amendment与source/static tests；②定义已声明capability/profile的目标语义；③形成source/protocol逐字段差异与最小责任路径；④冻结正常/重放/冲突/计划缺陷路由验收oracle。

allowed逐文件（所有basename均以 `docs/testing/planning/governance/` 为根）：`ALIGNMENT.md`、`differences.json`、`acceptance_contract.json`、`validate_contract.py`。source仅只读，不把静态测试写成运行证明。设计裁定由项目现行权威审批，Planner不冒充Adjudicator；审批事件来源在未来合同绑定。

验收：`C:/Python314/python.exe docs/testing/planning/governance/validate_contract.py --self-check`、`--verify-contract`，独立核验returned/reviewer_blocked的source verdict/step/finding/provenance、assignment/projection、幂等和错误码契约。缺权威批准、实现/协议不一致未登记、预设bridge缺失/已通过、编造capability均拒绝。

成功：得到明确批准的语义与差异责任，而不是要求用户手工改库。对齐完成门禁覆盖AGENTS、protocol、amendment、实际candidate能力与实现：批准源码语义时TP-05同步所有现行权威文本；批准协议语义或两者需改时，必要产品修复按P-FIX加入TP-09/11G前置。协议变化交TP-05；生产修复由Planner另冻daemon/client独立任务及回归合同，所有适用对齐责任未闭合不得冻结下游合同或宣称运行通过。

## 5. 下游条件合同的冻结要求

下列要求连同§2共同合同适用；每卡正式冻结必须将“相关入口/场景/路径”展开为精确清单、至多五个实现步骤及可执行命令。多语言/外部工具/多平台卡若出现多个独立owner或超过五步骤，Planner在执行前按语言/依赖/target分卡，本文不给Executor自行拆分权。

| 卡 | 核心验收与负向义务 | 命令与未冻结项 |
|---|---|---|
| TP-05 | 只更新批准协议语义，不静默覆盖历史；Role Prompt来源/hash/cutover影响核验，发现代码变更另拆，不在此编辑源码 | 依TP-04列精确文档校验命令；模板/Skill改动若必要另列whitelist，不默认授权 |
| TP-06 | Python扩展/daemon候选及离线依赖；binary/import路径/hash、Python ABI与Rust target可追溯；缺工具/错来源失败 | 按BUILD_ENV与TP-02冻结build命令，daemon --no-default-features；全部build在run根内批准源快照副本执行，release/build.py写根扩展/target亦留在副本，缓存/venv/target禁止落回共享仓库；不得以扩展构建冒充daemon |
| TP-07 | bind 0、source/hash/PID/manifest一致；日志排空、启动失败/超时/退出/强杀cleanup；禁止production发现和联网startup | 拟selector tests/acceptance/harness/bootstrap；HTTP/UDS/Pipe按承诺profile拆适配合同，不预设只有HTTP |
| TP-08C | 子命令、standalone、flags/aliases/global及原生profile；default/choices/nargs/互斥/required/type提取；常驻入口安全提取 | inventory/cli提取脚本entrypoint与selector在TP-07 API稳定后冻结；不调用真实业务做help提取 |
| TP-08M | ClientSession initialize/list分页、runtime schema集合/字段；stdout协议、startup隔离；重复/缺工具/未知schema拒绝 | inventory/mcp selector；真实子进程wire，不能用进程内manager替代 |
| TP-08F | 逐profile入口/参数义务、applicability批准依据、差异消除/版本hash；GAP-14闭合，未审漂移阻断 | frozen验证器与manifest文件名依TP-01契约冻结；暂不覆盖旧fixture和gen_test_cases消费链，必要适配另冻独立路径 |
| TP-09 | 空目录公开初始化/迁移/workspace/identity/contract/task/lease/snapshot/evidence；无SQL助手；重复初始化/无权限/authority不符失败 | initialization selector与具体公共RPC由批准协议/实际capability冻结；TP-04/05若要求产品修复，该独立修复通过后才可冻结/执行相关TP-09/11G，缺能力不能fixture补偿 |
| TP-10C/M | 每入口C1–C6适用义务、R1/R2真实实体、参数等价类、独立oracle、错误及前后状态、跨端映射 | 各自case manifest和完整expected selector；cw test仅受控模块，常驻命令ready/交互/stop；MCP业务写不是普通文本成功 |
| TP-11G/I | 真实独立身份/租约/治理闭环；A/B完整集合/OS用户/权限及traversal/symlink/ACL/限额/审计；越权不写 | domain selectors；用户/安全配置必须按profile冻结，不以未声明未来auth能力拒收当前profile |
| TP-11J/E/W | jobs到terminal与结果落地；内容hash/scope/patch/回滚；watch改删/rename/branch/overlay；取消重试、竞态冲突 | 各domain selectors，列故障注入公开/系统路径，不以monkeypatch假故障代表真实进程路径 |
| TP-11S | CAS四阶段/crash/pending refs/GC与refresh竞态、WAL/迁移/恢复；数据及事件oracle | storage selectors，冻结破坏点/恢复步骤；不同进程/临时库内做，禁止删除生产WAL/SHM |
| TP-11L/X | 16语言定义/调用或引用/残缺语法/Unicode/跨文件增量；真实外部工具可用与不可用契约 | 每语言/依赖独立适用矩阵与selectors，HCL按引用oracle；stub不计真实功能 |
| TP-12 | 同candidate干净安装/升级/重启/恢复，实际entrypoint/扩展/daemonhash、平台产物验收 | release selectors及各target runner；无对应产物不得用源码debug证据代替 |
| TP-13 | 递归JUnit+业务报告+expected obligations；缺/空/重复/中断/错版本/UNKNOWN/collection error拒绝，必需skip/xfail=0 | gates selector及独立CLI入口在report schema冻结后定；合成坏报告验证fail-closed，再验真实输出，不能只数函数 |
| TP-14 | R3真实仓库cold/warm/end-to-end/storage，资源/吞吐/增量与长期稳定；固定环境重复测量、统计与预算 | performance selectors、样本数/分位/噪声/timeout预算由TP-02冻结；不得临时调整预算补绿 |
| TP-15 | PR/nightly/release职责和profile覆盖，同candidate hash聚合，失败传播/工件完整、旧证据失效 | 真实CI日志/job rc/gate报告；workflow语法通过不等于CI通过；其他release workflow修改若必要先重冻白名单 |

命令政策：未来VCS/build/test按TokenSlim上下文及AGENTS执行（例如 `tokenslim run C:/Python314/python.exe -m pytest <该卡冻结selectors> --junitxml=<该run报告>`）。占位命令不能直接验收；Linux/macOS用冻结平台解释器，Windows显式C:/Python314/python.exe。TP-06/12/15须列全部确切命令、cwd/env、expected rc与报告路径后才可派工。

## 6. 并行、集成、去重与修复

TP-01/02/03/04新目录互不相交，可并行设计；TP-05在04后，06在01/02后，07接06。08C/08M、10C/10M及各11允许在**独立隔离实例与独立目录**并行；不能共用临时库/lease/endpoint。消费方只读harness公开API，需要改共享底座交其owner并串行新修订，不能修改其conftest偷偷补前置。

C卡负责全部入口基础语义；D卡负责明确domain附加场景，case ID稳定且selector/证据唯一归属，复用oracle/helper只读。跨端oracle映射由TP-01定义，10C/M分别产出，由13合并；同一nodeid或重复运行不能扩大分子。D专项缺case不得以C的同名工具调用补绿。

预期新增tests/acceptance目录无需先修改旧tests/convergence/conftest或全局tests/conftest；如发现pytest自动加载污染，07先记录影响并由Planner追加精确适配scope。现有convergence继续作为分层历史套件，其迁移、去skip、M1命名修正、T2/T3阈值调整均需要后续明确白名单，不能吸入无关卡。

所有拟新建.py/.rs按AGENTS规则47控制行数，按职责分文件，不新建超1500行文件；超800须说明拆分。当前草案不授权修改cli/main.py、task_collab_lifecycle.rs等巨型生产文件。生产缺陷出现后Planner先做行数/影响半径/ownership预检，独立修复合同中决定拆分或技术债，不能借验收卡顺带修改。

条件修复节点P-FIX：由TP-04目标语义或任一基建/验收finding触发，Planner记录source finding、受阻任务、source revision与具体能力缺口，冻独立daemon/client ownership、逐文件whitelist、最多五步骤、回归oracle、证据及回滚。每个P-FIX必须在受阻卡前置中显式追加并独立审查完成，不能以“发现后再说”继续派工；无finding不凭空创建生产任务。具体路径待可复核finding决定，当前不授权任何生产修改；此节点不代替同任务内普通测试实现缺陷的fix_defect。

## 7. 审计与入库前门禁

其他Agent审计：①是否遗漏公开入口/专项；②DAG是否无环且前置够用；③ownership和allowed文件是否相交；④首批合同是否有独立可验证交付/失败标准；⑤后续条件合同是否误称已冻结；⑥是否有生产权威旁路/虚假证据；⑦平台/性能/离线资产缺口是否显式；⑧修复是否超scope。

入库前必须：用户确认策略；本稿独立审计问题闭环；重新冻结source+方案文件hash/dirty归属；明确首批实际owner与真实身份策略；查询daemon现行原子创建/Contract/binding能力并制定受支持注册路径。当前未查询daemon，因此不宣称支持Planner原生派工。技术能力缺口由Planner另规划最小修复，不能要求用户改库或伪造binding。

本轮Handoff：独立Agent只读审计此离线草案，结果交Planner修订，再供用户确认。未创建task或调用task split/create。
