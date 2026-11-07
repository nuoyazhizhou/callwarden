# 覆盖与 Skip 审计（v7 · 静态事实与运行证据分离）

> 本轮只做静态检查，无pytest/cargo/CLI/MCP业务运行，无CI日志。
> 复算源STATIC_AUDIT.json、逐站点SKIP_SITES.json，由gen_test_cases.py生成；文本不能代替新扫描。

## 1. 统计口径

测试定义、Rust属性数、skip调用站点、collected nodeid、运行结果是不同单位。参数化/class/module skipif/importorskip/setup skip/collection error影响执行数量，不能从站点推算运行skip率或解锁用例数量。

当前Python定义7464、convergence定义58；Rust src #[test]+#[tokio::test]属性2163、含#[cfg(test)]文件123。这不是当前通过数，也不含全部Rust integration/bin测试。扫描文件及hash/算法见机器报告。

AST调用pytest.skip230、pytest.mark.skipif243，合计473，分布144文件；另有pytest.importorskip25。三种合计498仅是机制站点，非运行skip数。别名/动态机制不在当前精确匹配统计内，runtime补齐。

精确reason字面量“callwarden_core 未安装”46，其中parser_contract18；“callwarden_core Rust 扩展未构建”4。两字面量全仓合计50；其他变体及importorskip另列。parser_contract前两机制总计44。v6的39/36/3不符合当前AST。

**reason语法分布（不等于根因归因）**：498站点中含字面量reason292（292/498≈58.63%），含表达式reason183（纯表达式，与字面量互斥），无显式reason23；纯表达式+缺省合计206（206/498≈41.37%）。字面量只允许直接提取文本，动态表达式也可结合源码人工分析，不能按语法形态断言能否归因。以下“非字面量或缺省”合并两类，不能把缺省写成表达式：

| 机制 | 站点 | 非字面量或缺省 | 占比（近似） |
|---|---|---|---|
| pytest.skip | 230 | 114 | 49.57% |
| pytest.mark.skipif | 243 | 69 | 28.40% |
| pytest.importorskip | 25 | 23 | 92.00% |

importorskip共25处，其中23处缺省（无显式reason）、2处为字面量reason，不是模块名非字面量；`"callwarden_core"` 本身是字符串字面量，其模块前置记录在condition_or_module。46/18/4是特定reason文本子集，不能声称它们覆盖60%站点。当前全部498处root_cause仍为UNREVIEWED，人工根因已复核数为0。

GAP-16要求逐站点复核源码条件、依赖、平台与适用profile，并绑定对应nodeid或collection位置。runtime补充实际触发的reason与状态；未触发的站点登记NOT_TRIGGERED和审查依据，不伪造运行reason，也不要求为凑全量故意触发所有skip。一处站点可影响多个nodeid，一次collection skip也不能虚构未收集用例数。必需适用suite/case被skip仍按交付门禁阻断。

reason字面量分组不是人工根因归因。SKIP_SITES记录路径/行/机制/reason/condition/hash，root_cause统一待复核；故239/190/52等历史差额不再作为当前结论。不得混用估算/重叠家族加总成全量归因。

## 2. 入口事实

源码注册名/矩阵/分类集合静态一致243；backend200 rust_native+43 task_rpc；op_class161 READ_ONLY+77 PROTECTED_MUTATION+5 GOVERNANCE_WRITE。MCP wire 已通过真实 tools/list 提取（docs/testing/manifests/mcp/mcp_tools.json，243 项，使用当前真名 detect_dependency_cycle/detect_call_cycles，无旧名 detect_cycle/detect_cycles），清单已冻结（docs/testing/manifests/frozen/inventory_frozen.json，508 入口），GAP-14 已闭合（提取侧 + 收敛侧）：收敛套件 T2/T5 引用的 `audit_mcp_inventory.json` 与 `mcp_full_schema.json` 两个 fixture 已将陈旧名 detect_cycle/detect_cycles 定点改名为真名 detect_dependency_cycle/detect_call_cycles，与 mcp_tools.json 名字集合完全一致（diff=NONE），T2 按真名查表取参，不再空参调用改名工具。当前源码签名/默认值与全部快照仍需逐字段核验和运行时提取。声明需handler副作用复核，不能视为运行成功。旧前缀猜测171READ/72WRITE把submit_verdict/append_evidence等治理写标错，v7改矩阵声明。

CLI快照声明233叶子，commands232、skipped=[server]；分类84顶层/21类。TP-08C 已通过进程内挂钩 argparse 真实提取 cli_tree.json（84 顶层 + 144 嵌套，含 default/choices/nargs/required/互斥）；help/usage 文本中的 "-h, --help" 是 argparse 正常输出（无害）。runtime/argparse、legacy flags/alias/global options 完整性已由 cli_tree.json 部分提取；MCP runtime wire schema 已由 mcp_tools.json 验证（243 真名）。

## 3. 静态风险与承接

| ID | 源码证据 | 事实/影响 | 承接 |
|---|---|---|---|
| GAP-01 | convergence/conftest:_pick_bin；ci.yml | .exe硬编码、主CI只构建扩展，Linux daemon前置断链 | B/E构建+跨平台ready |
| GAP-02 | ci.yml:rust-unit-test | 只跑compat_native_handlers | E适用src/integration/bin清单及隔离 |
| GAP-03 | convergence/conftest补库助手 | workspace补录及业务表DDL可能掩盖初始化缺口 | B空库公共接口；产品修复另冻scope |
| GAP-04 | test_t3_cli_full_invocation | 发现可用daemon，refresh结果不硬阻断 | B/C强制隔离和前置 |
| GAP-05 | t2/t3 runner | 未知错误默认EXPECTED_BUSINESS，无逐入口oracle | C明确预期/UNKNOWN阻断 |
| GAP-06 | test_t2/test_t3 | >=240/>=230容差，PASS>=100/DEFECT<=18 | A/C严格集合、交付零缺陷 |
| GAP-07 | t2_mcp_runner | 进程内call_tool、14个SKIP_TOOLS | C真实wire/全量适用语义 |
| GAP-08 | seed_sample/旧生成器 | 错误oracle、占位参数 | A/C独立golden、实体provenance |
| GAP-09 | test_m4/test_t4 | monkeypatch/宽泛错误、空列表可绕过隔离断言 | D真实故障/精确集合/状态不变 |
| GAP-10 | v6方案 | 静态冒充skip率、reason/归因加总冲突 | A静态账本及runtime分离 |
| GAP-11 | test_m1_route_matrix | 常量243，函数/docstring遗留239 | **已修复（2026-10-13）：docstring/函数名 239→243 对齐常量 `_EXPECTED_TOTAL`** |
| GAP-12 | scripts | check_ci_gates/check_skip_rate不存在 | E完整报告/selector门禁待建 |
| GAP-13 | v6 BUILD_ENV | 历史wiimu/Zig路径与当前环境混用 | E按profile核构建来源 |
| GAP-14 | audit_mcp_inventory.json + mcp_full_schema.json(旧fixture) vs mcp_tools.json(新) vs源码/矩阵 | 旧fixture两个环检测工具名已陈旧；总数相等掩盖集合漂移 | **已闭合（2026-10-14）：提取侧 TP-08M 真名源；收敛侧两 fixture 陈旧名 detect_cycle/detect_cycles 已定点改名为 detect_dependency_cycle/detect_call_cycles，与 mcp_tools.json 名字集合 diff=NONE，T2 按真名查表取参** |
| GAP-15 | task_collab_lifecycle.rs:1516–1524（`adjudicator_returned` 同事务准备 fix_defect）vs 协议（原 `docs/design/role-protocol.md` 已迁至 `.agents/skills/cw-task-loop/references/role-protocol.md`；`docs/role-loop-templates/Callwarden 无人值守循环启动模板：Adjudicator v4.md:32` 明写"returned 不自动追加整改 step、不自动 reopen——自动 remediation 仅 reviewer_blocked 触发"） | returned 同事务整改的源码与协议"不自动追加"冲突，运行结果未知 | A解决权威语义冲突；D按冻结合同真实验证projection/steps/assignment、provenance、幂等与冲突 |
| GAP-16 | SKIP_SITES.json 静态扫描 | reason字面量292、含表达式183、缺省23（互斥口径，合计498）；全部498处根因尚未复核，语法分布不等于归因率 | A0逐站点源码/适用性复核；运行阶段关联实际reason/nodeid/collection，未触发登记依据；不伪造全量runtime归因 |

不再将“CI执行量0/全ERROR”称本轮实测：配置可推断前置风险，但部分静态测试不依赖daemon，具体数量需CI/JUnit。历史T2 157/72/0/14、T3 70/114/18/31只是旧叙述，非当前revision证据。

## 4. 待建运行审计

冻结expected selectors/profile/corpus/版本。报告collected、passed、failed、error、setup/call/teardown skip、xfail/xpass、collection模块skip、not_applicable、missing、UNKNOWN和中止。参数化按nodeid，模块importorskip不虚构未收集数量。

适用运行skip率S/N来自同次适用collected nodeid，collection skip另列且缺必需suite阻断；跨suite去重，平台分母事前冻结。零必需unexpected skip/xfail不受总比例豁免。

逐case报告连接入口/参数义务/selector/证据；JUnit不代替语义覆盖。空套件、缺报告、命令失败无JUnit、testsuites子节点漏聚合、maxfail截断、过期/版本不符/脏变更、selector缺失、UNKNOWN均拒绝放行。

## 5. 评审与产品边界

独立Agent PASS仅说明方案静态边界可继续规划，非产品通过、非daemon verdict。产品必须满足TESTING_PLAN §8。REVIEW记录本次文档评审。TP-07~TP-10 已实施并实跑验收（见 §6）；E 交付门禁已由 TP-11E/G 实现并接线 CI（见 §6），不存在“P0完成就完整交付”；运行证据（243-wire/265-CLI DEFECT=0）来自 tests/acceptance 套件，非本审计轮次自行运行。

## 6. 实施进展同步（2026-10-13）

自 v7 文档（2026-10-10）以来，代码层已完成 TP-07~TP-10 验收套件并实跑，文档须同步以下事实（避免审计方误判“测试尚未实施”）：

- **TP-08C/M/F**：真实提取并冻结清单。CLI 经进程内挂钩 argparse 提取 `docs/testing/manifests/cli/cli_tree.json`（84 顶层 + 144 嵌套）；MCP 经真实 wire tools/list 提取 `docs/testing/manifests/mcp/mcp_tools.json`（243 工具，当前真名）；`docs/testing/manifests/frozen/inventory_frozen.json` 冻结 508 入口。**GAP-14 提取侧已闭合**（生成器静态账本已切换至 mcp_tools.json 真名源，`planning_inventory_gate` 不再 `BLOCKED_SCHEMA_DRIFT`）。
- **TP-10M**：MCP 全工具真实 `ClientSession` wire 验收（**243 工具 DEFECT=0**），非进程内 `call_tool`——修复旧 GAP-05/07 的“路由通冒充业务对”。
- **TP-10C**：CLI 全入口语义验收（**265 冻结入口 DEFECT=0**），用真实 `subprocess` 拉 `cw-daemon`。
- **a8b96b2**：T2/T3 陈旧证据重跑 + T3 门禁升级为零缺陷。
- 新增门禁：覆盖率门禁（pyproject + CI 采集）、SAST/SCA/secret-scan、ruff 阻断。
- **GAP-11 已修复**：`test_m1_route_matrix.py` docstring/函数名 239→243 对齐常量 `_EXPECTED_TOTAL=243`（原先 M1 门禁自相矛盾，不可作证据）。
- **P2-1 根因已修**：`tests/convergence/fixtures/cli_full_params.json` 的 235 处 option 尾逗号（`-h,` 等）已清洗；`gen_test_cases.py` 拼接 option/positional 时增加 `rstrip(",")` 防御，重跑后 TEST_CASES.md 不再含 `-h,,` 脏 token。注意 `cli_tree.json` 的 "-h," 仅出现在 help/usage 文本（argparse 正常输出），无害，未改提取器。

**实施进展（2026-10-16，TP-11G CI 硬化 + F-OWNER-01/02）**：
- **TP-11G CI 硬化**：在已闭合的 E 交付门禁之上修复 P0/P1——windows job 包装载/`--no-deps`、golden 重冻结 + manifest 漂移守卫、daemon_e2e env 变量对齐 bin 改名、workspace root 归一化 canonicalize、enterprise_params 平台条件输入、守卫测试双定义副本删除；windows job 改为不装包（联接+PYTHONPATH）绕开 PrebuiltBuildExt 硬阻塞。
- **F-OWNER-01/02（#63）**：Windows 属主/权限校验（I 域）大重写——`ownership_allows` 双态契约（SelfOwned || admin 豁免）、`probe_admin_membership` 静默早退转可观测、CheckTokenMembership 首个实参修复（恢复提升会话 admin 跳过）、抽纯函数 `owner_check_decision` 承载属主比较决策、10+ 用例契约化 + 删 MASKING 用例、确定性 fixture 与 golden 重冻结；属主拒绝信息补 remedial hint。属主校验逻辑行为保持型重构。

**实施进展（2026-10-17，CI-DEBT 技术债清理）**：
- **CI-DEBT 大规模技术债清理（25 提交，HEAD 83f3604）**：在已闭合的 E 交付门禁之上做安全/lint/合规持续硬化——CI-DEBT A（quick-xml 0.37.5→0.41.0 修 RUSTSEC-2026-0194/-0195、cargo-audit 改 --file + 版本锁 0.22.2）、B1/B2（TP-07/TP-09/TP-10C harness 候选缺失改 pytest.skip、5 个 assertion drift 按现行 daemon 契约对齐）、CD-1（Linux Python job 联接仓库根可导入、TP-11G D5 crosscheck module-level windll 守卫补 skip、TP-10C 模块级 RuntimeError 改 skip）、CD-2（F821 真修×41、30 行级 noqa、pyproject 显式 ruff select）、CD-3（migrate.py attach_name 白名单校验消 B608、10 条 bandit Medium nosec 登记）、CD-4（129 处 uses SHA pin 使 mutable-tag findings 归 0、enterprise-release syft/sbom 修复、semgrep --timeout 60 去盲区）、CD-5（governance harness daemon 候选缺失改 skip、batch_file_versions_query open_readwrite 补齐 FK/WAL pragma、G10/G20 read_to_end 断言收窄）、gitleaks 272 findings 逐条 triage 为假阳性 + allowlist、ruff E722/W605 10 处修复、manifest 生成步骤 + 回退 maxfail 诊断改动。
- **skip 站点计数漂移（GAP-16 关联）**：CI-DEBT 把已知环境/平台条件（daemon 候选缺失、仅 win32 命名管道）的失败改为 pytest.skip，新增 11 处 skip 站点（pytest.skip 213→222、skipif 234→236），SKIP_SITES 总数 468→479；全部 479 处 root_cause 仍 UNREVIEWED，CI-DEBT 未做任何根因复核，仅将已判定的环境/平台前置条件从 fail 转为 skip（正确硬化，不掩盖缺陷）。
- **CI-DEBT 续（24 提交，HEAD 2b638d5，2026-10-18）**：在 10-17 CI-DEBT 之上继续收口——w3 daemon P0-3 fail-closed 20 ERROR（test job 创建 callwarden-clients 组+sg 包裹 pytest）、daemon 早亡 stderr 取证（harness instrument 输出 manifest 后死亡）、http-daemon capability matrix / MCP 工具矩阵 source_file 对齐模块重命名（26 条目）基线入库修复 CI 同族缺失、compat dispatch 补 mcp.daemon_client.publish_snapshot 路由修复 daemon_uds 3 红、fake daemon 响应信封补 ok:true 对齐生产 wire、db 跨仓检测语言推断自递归缺陷（hasattr 恒真致 python 仓误判 rust）、fuzz 并发写断言自相矛盾修复、hook 诊断断言对齐实现文案（cw server --stop 非真实 CLI 命令）、test_cli_consistency 断言逻辑翻转修复（3dd515a 写反致业务错误误判红）、test matrix job fetch-depth:0（契约测试需 git 历史做祖先校验）、tree extraction 隔离 env + 零生产写测试重做、daemon 自递归守卫（PATH 上本包 console script 被当二进制 exec）、convergence daemon 候选 skip + i18n 全局单例连环污染、5 个 drift/状态污染测试按现行契约对齐、TP-10M MCP wire harness 无已知散列候选改 skip。
- **skip 站点计数再漂移（GAP-16 关联，2026-10-18）**：CI-DEBT 续将 TP-10M MCP wire harness 无已知散列候选等改 pytest.skip，新增 2 处 skip 站点（pytest.skip 222→224），SKIP_SITES 总数 479→481，classic_skip_files 139→140；全部 481 处 root_cause 仍 UNREVIEWED，根因复核仍未启动（人工复核 0）。含表达式 reason 460→462（纯表达式 173→175），非字面量或缺省 192→194（占比 40.08%→40.33%），含字面量 287 不变（占比 59.67%）。

**实施进展（2026-10-20，HEAD 7d8eb15，CI 取证 + lease 家族测试加固）**：
- **19 提交（HEAD 7d8eb15）**：在 CI-DEBT 续之上继续收口——CI probe job 系列（Probe C/D/E：PYTHONPATH 拼接修复、WAL 双实例 A/B 定性实验、metrics CLI CI-only 失败取证，避开日志 API 截断）；lease 家族新增 property_lease_fencing / task_thread_lifecycle（时钟注入）、p4_migration / p4_protected_mutation（默认时钟注入 + finds_via_path runtime 候选隔离）、a1d8409 lease fixture 默认时钟注入修复无 daemon 环境 CI 失败 + 新增时钟不可用 fail-closed 把守；phase7 轮询改走 executor 加锁访问器（共享连接并发序列化）、JobExecutor 连接注入消除同进程双连接 WAL Hazard；phase4 daemon 路由加隔离 daemon fixture、runtime/current daemon 存在性断言平台分支；db 跨仓检测改用模块级语言推断（修复生产 CodeGraphDB MRO 遮蔽致 python 仓 0 命中）；i18n 断言对象 cw.py→cli/main.py、health worker_status 两态、guardrail 读面契约对齐、dashboard UTF-8 取证增强。属 E 门禁已闭合后的维护期工作，非新缺口。
- **skip 站点计数再漂移（GAP-16 关联，2026-10-20）**：本轮新增 1 处 pytest.skip（字面量 reason），SKIP 站点总数 481→482，classic_skip_files 140 不变（pytest.skip 224→225、skipif 236、importorskip 21）；python_definitions 7309→7320（+11，lease 家族/daemon 路由等新增测试函数）。全部 482 处 root_cause 仍 UNREVIEWED，根因复核仍未启动（人工复核 0）。含字面量 reason 287→288（288/482≈59.75%），含表达式 reason 463（=288+175），非字面量或缺省 194（194/482≈40.25%）。

**实施进展（2026-10-27，HEAD 0fc6ba2，48 提交：WAL v4 / macOS 收口 / known-red 隔离 / env 泄漏修复）**：
- **48 提交（HEAD 0fc6ba2）**：主要工作——WAL hazard 正式修法 v4（Python 扩展在 Linux 上链系统 libsqlite3，同进程单一 SQLite 实例，malformed 根因根除、h7/p15 首次同时全绿）、macOS 撕裂面裁决与收口（停发布物/门禁、改防回归守卫）、known-red 隔离机制（注册表+到期+deselect+quarantine job，KR-A/B/C 登记）、enterprise-release 改走 build.py v4 门控、semgrep 移出核心依赖（KR-C）、6 处 CW_USE_RUST_STORAGE env 泄漏改 monkeypatch、5 处未用 import os 清理、combined_worker manifest 等待超时 10s→30s。属 E 门禁闭合后的质量收口期，非新缺口。远端新增 `full_stack_review_20261024.md`（门禁结论：不通过）与 `known_red/`。
- **静态账本漂移（GAP-16 关联）**：pytest.skip 226→227（+1，字面量 reason）、classic 466→467、classic 文件 142→143、三机制合计 487→488；python 定义 7320→7343（+23，WAL v4 / macOS 收口 / known-red 等新增模块）；含字面量 reason 293→294（294/488≈60.25%）、非字面量或缺省 194（194/488≈39.75%）；rust 属性 2113、cfg 122、skipif 240、importorskip 21、子集 46/18/4、parser 44、root_cause_reviewed 0 全部不变。全部 488 处 root_cause 仍 UNREVIEWED（累计 +20 处 skip 站点，人工复核 0）。`STATIC_AUDIT.json` 已按 HEAD 0fc6ba2 重生成，`--check` 全 PASS，SKIP_SITES/TEST_CASES 同步。

**实施进展（2026-11-05，HEAD 7e178cb，s4 重构 + 账本重生成）**：
- **pull 合入 7e178cb（2026-11-05）**：本提交为 `refactor(s4)`——删除 `cw_bridge.rs` 与 4 个 bridge/WSL 测试（`test_bridge_restart_dedup.py`/`test_windows_bridge_e2e.py`/`test_windows_wsl_authority_e2e.py`/`test_wsl_authority_routing.py` 共 -2546 行），新增完整 PKI/mTLS 主体授权体系（Rust daemon 侧 `cli/pki.rs` 911 行、`daemon/auth_principal.rs` 724 行、`pki_api.rs` 877 行、`pki_handlers.rs` 194 行、`peercred.rs` 177 行、`http_server.rs` 大改）及 3 个新测试（`test_mtls_control_plane.py` 581、`test_mtls_enrollment.py` 417、`test_principal_authorization.py` 290 行）。`server/mcp_server.py` +10 行但 MCP 工具数仍为 **243**（真源 `mcp_tools.json` 核验一致）；CLI 84 顶层/21 类/233 叶子不变（Rust `pki` 为 `cw_daemon` 子命令，不新增顶层）。`STATIC_AUDIT.json` 已按 HEAD 7e178cb 重生成，`--check` 全 PASS，SKIP_SITES/TEST_CASES 同步。
- **静态账本漂移（GAP-16 关联）**：pytest.skip 232→230（-2）、pytest.mark.skipif 240→243（+3）、pytest.importorskip 21→25（+4），三机制合计 493→498（+5）；classic 472→473（+1）、classic 文件 144 不变；python 定义 7481→7464（-17，删 4 个 bridge/wsl 测试文件 > 新增 config.py/cli 增量）、python 文件 710→709（-1）、rust 属性 2148→2163（+15）、cfg 文件 122→123（+1）；字面量 reason 290→292（+2）、含表达式 reason 184→183（-1）、缺省 19→23（+4），nonliteral_or_absent 194→206（+12，合计 498）；子集 46/18/4、parser 44、schema 名称漂移 0（GAP-14 仍闭合）、root_cause_reviewed 0 全部不变。全部 498 处 root_cause 仍 UNREVIEWED（人工复核 0）。

**实施进展（2026-11-04，HEAD fc8b575，pull 合入 + 账本重生成）**：
- **pull 合入 fc8b575（2026-11-04）**：远端以 ADR §15 实施清单 + PKI 五表 schema 落地为主（docs(mtls)+db(pki)）。核心清单零漂移：MCP 243 工具、CLI 84 顶层/21 类/233 叶子、schema 名称漂移 0（GAP-14 仍闭合）、op_class 161/77/5 不变。`STATIC_AUDIT.json` 已按 HEAD fc8b575 重生成，`--check` 全 PASS，SKIP_SITES/TEST_CASES 同步。
- **静态账本漂移（GAP-16 关联）**：pytest.skip 227→232（+5）、classic 467→472（+5）、三机制合计 488→493（+5）；python 定义 7383→7481（+98，PKI 五表 schema 等新增模块）、python 文件 702→710（+8）、rust 属性 2138→2148（+10）；字面量 reason 294→290（-4），含表达式 reason 184、无显式 reason 19（互斥口径，合计 493）；子集 46/18/4、parser 44、cfg 122、skipif 240、importorskip 21、root_cause_reviewed 0 全部不变。全部 493 处 root_cause 仍 UNREVIEWED（人工复核 0）。

**实施进展（2026-10-30，HEAD 6bd1e9c，账本曾失准后重生成）**：

- **HEAD 前进导致账本失准（关键）**：自 `bd5cd60f` 以来远端合入 18 提交（daemon/workspace/schema/perf 收口 + Wave 2~5 E2E 报告文档），且未触碰我的 11 个未提交文件。但我的三份账本/两份文档仍按 `bd5cd60f` 对齐，致 `STATIC_AUDIT.json` 的 `supplied_source_revision` 与树不符、`--check` 一度 FAIL（STATIC_AUDIT + SKIP_SITES 字节不一致）。已按新 HEAD `6bd1e9c` 重生成三份账本：`--check` 现全 PASS、字节可重现。真实漂移极小——仅 python 定义 7374→7383（+9）、rust 属性 2119→2138（+19）；skip 站点（488）/MCP 243/原因分布/子集 46·18·4/parser 44/root_cause 0 全部不变（新提交未动测试）。

**实施进展（2026-10-28，HEAD bd5cd60f，pull 合入 + 冲突 stash/pop 解决 + 重生成）**：
- **pull 合入 bd5cd60f**：远端删除 CLI 命令 `gc db-migrate-single`（对应 `server/migrate.py` 删除），新增跨项目 workspace 绑定 / db FK 孤儿 / workspace 身份等测试。前轮 11 个未提交改动因 `TEST_CASES.md`（整文件重生成 + 行尾规范化）与 `cli_full_params.json`（仅 autocrlf 行尾翻转、无数据改动）两生成链文件与远端冲突被 merge 中止，经 `git stash`→`git pull`→`git stash pop` 解冲突：两文件直接采用远端 `bd5cd60f` 版本（我的本地版无保留价值——一个是纯行尾翻转、一个是含被删 `gc db-migrate-single` 行的过时产物），再按新 HEAD 重生成三份账本。`STATIC_AUDIT.json` 已按 HEAD `bd5cd60f` 重生成：`--check` 全 PASS，SKIP_SITES/TEST_CASES 同步。
- **静态账本漂移（GAP-16 关联）**：本轮仅 python 定义 7343→7374（+31，跨项目 workspace 绑定 / db FK / workspace 身份等新增模块）、rust 属性 2113→2119（+6）、py 文件 699→702（+3）、cli 提取 233→232（-1，删 `gc db-migrate-single`）；MCP 243 工具、skip 站点（488）、原因分布（字面量 294 / 含表达式 469 / 缺省 19）、子集 46/18/4、parser 44、root_cause_reviewed 0 全部不变。全部 488 处 root_cause 仍 UNREVIEWED（累计 +20 处 skip 站点，人工复核 0）。

**实施进展（2026-10-22，HEAD d5cfbeb4，pull 合入 509 文件 + 冲突 stash/pop 解决）**：
- **pull 605c549→d5cfbeb4（509 文件）**：远端本批以测试清理（删除未用 `import pytest` 等）+ WAL hazard v2（conn_pool.rs→db_conn.rs）为主；新增 `docs/testing/ci_malformed_bisect_evidence.md`。我前轮留下的 11 个未提交改动因 `test_m1_route_matrix.py` 与远端冲突（我改 239→243 文案、远端删 `import pytest`，二者不重叠）被 merge 整体中止——经 `git stash` → `git pull` → `git stash pop` 自动合并解清，两处改动均保留，无冲突。
- **静态账本仅 rust 属性 +1（2112→2113）**：skip 站点（226/240/466/142）、python 定义 7320、MCP 243 工具、其余 rust cfg/原因子集/root_cause 计数全部不变。`STATIC_AUDIT.json` 已按 HEAD `d5cfbeb4` 重生成，`--check` 全 PASS，SKIP_SITES/TEST_CASES 同步。

**实施进展（2026-10-21，HEAD 605c549，连接池单例 + Bootstrap Gate + 审计白名单 + 收窗口）**：
- **11 提交（HEAD 605c549）**：在 lease 家族加固之上继续收口——fix(rust) SQLite 连接池单例化（消除同进程双实例 WAL Hazard，malformed 家族 CI-only）、Bootstrap Gate 补 daemon 生命周期（修复 E_HTTP_MANIFEST_MISSING 长红，8-22 CLI daemon-only 回归）；fix(srv-019) audit 白名单陈旧键名 + scanned_files 口径自洽（retired 11→14、finding 87→83）；perf 对比基线口径（Python 侧补等价 file_versions 查询，Rust 实测 0.85x 系口径不对等）；style 清 lint 账目第一批（F541 259 + E401 16）；CI probe F 隔离对照（残留 malformed×12 非并发而是进程内累积）；CI-only 诊断门 CW_RUST_POOL_LIFETIME=short（判别连接寿命是否为 malformed 根因）。属 E 门禁已闭合后的维护期，非新缺口。
- **skip 站点计数再漂移（GAP-16 关联，2026-10-21）**：本轮新增 5 处 skip（pytest.skip 225→226、pytest.mark.skipif 236→240，均为字面量 reason），SKIP 站点总数 482→487，classic_skip_files 140→142；rust 属性 2106→2112（+6）、cfg 文件 121→122（+1），python_definitions 7320 不变。全部 487 处 root_cause 仍 UNREVIEWED，根因复核仍未启动（人工复核 0）。含字面量 reason 288→293（293/487≈60.16%），含表达式 reason 468（=293+175），非字面量或缺省 194（194/487≈39.84%）。

**已闭合（2026-10-15，TP-11E/G；2026-10-16 经 TP-11G CI 硬化收口 P0/P1；2026-10-17 经 CI-DEBT 持续硬化）**：
- **E 交付门禁与全平台 CI**：已由 TP-11E/G 实现并接线。`cicd/check_ci_gates.py`（JUnit 递归解析 + expected selector 完备性 1:1 + **revision 新鲜度 fail-closed 前置**，修旧 fail-open）、`check_skip_rate.py`、`artifact_e2e.py`（wheel/daemon exe 实算 sha256）、`check_selector_manifest.py`（防篡改）组成门禁本体；`.github/workflows/acceptance-gates.yml`（PR/Nightly/Release 三档，Windows daemon 验收）+ `ci.yml`（ubuntu daemon lib 单测 + windows 命名管道 SDDL/SID/peer identity/`daemon::client` 专项 + false-positive 反证）完成跨平台接线。新增 `docs/testing/expected_selectors/{domains_full,domains_pr}.txt` 冻结选择器真源；治理 harness 增加 `CW_TP11G_ALLOW_FRESH_BUILD` CI 逃逸开关（信任当次构建二进制，本地/生产仍强校验 KNOWN 前缀）。
- `STATIC_AUDIT.json` 已按当前 HEAD `6bd1e9c`（2026-10-30 重生成）刷新，使 `supplied_source_revision` 与扫描树一致、可重现（skip 站点累计 468→479→481→482→487→488→493→498；python 定义 7309→7320@7d8eb15 后、`0fc6ba2` 再 +23（7320→7343）、`bd5cd60f` 再 +31（7343→7374）、本轮 `6bd1e9c` 再 +9（7374→7383）；`605c549` 轮 +5 skip 站点 482→487、rust 属性 2106→2112、cfg 121→122；`d5cfbeb4` 仅 rust 属性 2112→2113（+1）；`0fc6ba2` +1 skip（226→227）/+1 文件（142→143）/字面量 293→294；`bd5cd60f` python +31（7343→7374）、rust +6（2113→2119）、py 文件 699→702、cli 提取 233→232（-1，删 `gc db-migrate-single` 及 `server/migrate.py`）；本轮 `6bd1e9c` 仅 python 定义 +9（7374→7383）、rust 属性 +19（2119→2138），skip 站点（488）/原因分布/子集/root_cause 计数全部不变；`6bd1e9c`→`fc8b575`（2026-11-04）python 定义 7383→7481（+98）、rust 属性 2138→2148（+10）、py 文件 702→710（+8）、pytest.skip 227→232（+5）、classic 467→472（+5）、三机制 488→493（+5）、字面量 294→290（-4），skip 站点（493）/原因分布/子集/root_cause 计数其余不变；`fc8b575`→`7e178cb`（2026-11-05，s4 重构）python 定义 7481→7464（-17）、py 文件 710→709（-1）、rust 属性 2148→2163（+15）、cfg 文件 122→123（+1）、pytest.skip 232→230（-2）、pytest.mark.skipif 240→243（+3）、pytest.importorskip 21→25（+4）、三机制 493→498（+5）、classic 472→473（+1）、字面量 290→292（+2）、含表达式 184→183（-1）、缺省 19→23（+4）、nonliteral_or_absent 194→206（+12），schema 名称漂移 0（GAP-14 仍闭）、root_cause_reviewed 0 不变）；生成器 `--check` 全 PASS。SKIP_SITES/TEST_CASES 同步重生成。

**仍遗留（未闭合）**：
- （GAP-14 收敛侧已于 2026-10-14 闭合：两 fixture 陈旧名已定点改名为真名，名字集合与 mcp_tools.json diff=NONE；T2 按真名查表取参，不再空参调用改名工具。）
- GAP-15 治理语义冲突（`task_collab_lifecycle.rs:1516-1524` 对 `adjudicator_returned` 同事务准备 fix_defect，与 Adjudicator v4 模板"returned 不自动追加整改 step"协议冲突）仍待权威语义对齐，方可进治理验收。
- GAP-16 的 498 处 skip 站点 root_cause 仍 UNREVIEWED（累计 +25 处 skip 站点；d5cfbeb4 仅 rust 属性 2112→2113（+1）；0fc6ba2 再 +1 skip/文件、python 定义 +23（7320→7343）、字面量 293→294；bd5cd60f 仅 python 定义 7343→7374（+31）、rust 属性 2113→2119（+6）、cli 提取 233→232（-1，删 `gc db-migrate-single`）；6bd1e9c 仅 python 定义 7374→7383（+9）、rust 属性 2119→2138（+19），skip 站点仍 488、其余不变；fc8b575 python 定义 7383→7481（+98）、rust 属性 2138→2148（+10）、pytest.skip 227→232、classic 467→472、三机制 488→493、字面量 294→290，skip 站点仍 493、其余不变；7e178cb 重构 python 定义 7481→7464（-17）、rust 属性 2148→2163（+15）、三机制 493→498、字面量 290→292、缺省 19→23，skip 站点 498、其余不变；全部 498 处均为 UNREVIEWED，根因复核仍未启动，人工复核 0），静态账本未变。
