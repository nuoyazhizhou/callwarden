# 覆盖与 Skip 审计（v7 · 静态事实与运行证据分离）

> 本轮只做静态检查，无pytest/cargo/CLI/MCP业务运行，无CI日志。
> 复算源STATIC_AUDIT.json、逐站点SKIP_SITES.json，由gen_test_cases.py生成；文本不能代替新扫描。

## 1. 统计口径

测试定义、Rust属性数、skip调用站点、collected nodeid、运行结果是不同单位。参数化/class/module skipif/importorskip/setup skip/collection error影响执行数量，不能从站点推算运行skip率或解锁用例数量。

当前Python定义7133、convergence定义58；Rust src #[test]+#[tokio::test]属性2091、含#[cfg(test)]文件121。这不是当前通过数，也不含全部Rust integration/bin测试。扫描文件及hash/算法见机器报告。

AST调用pytest.skip213、pytest.mark.skipif234，合计447，分布131文件；另有pytest.importorskip21。三种合计468仅是机制站点，非运行skip数。别名/动态机制不在当前精确匹配统计内，runtime补齐。

精确reason字面量“callwarden_core 未安装”46，其中parser_contract18；“callwarden_core Rust 扩展未构建”4。两字面量全仓合计50；其他变体及importorskip另列。parser_contract前两机制总计44。v6的39/36/3不符合当前AST。

reason字面量分组不是人工根因归因。SKIP_SITES记录路径/行/机制/reason/condition/hash，root_cause统一待复核；故239/190/52等历史差额不再作为当前结论。不得混用估算/重叠家族加总成全量归因。

## 2. 入口事实

源码注册名/矩阵/分类集合本轮静态一致243；backend200 rust_native+43 task_rpc；op_class161 READ_ONLY+77 PROTECTED_MUTATION+5 GOVERNANCE_WRITE。固化MCP schema虽然也有243项，**名字集合漂移**：仍有detect_cycle/detect_cycles，缺当前detect_dependency_cycle/detect_call_cycles。清单冻结被阻断，缺失参数标未知，不复用旧名schema冒充当前契约。当前源码签名/默认值与全部快照仍需逐字段核验和运行时提取。声明需handler副作用复核，不能视为运行成功。旧前缀猜测171READ/72WRITE把submit_verdict/append_evidence等治理写标错，v7改矩阵声明。

CLI快照声明234叶子，commands233、skipped=[server]；分类84顶层/21类。runtime/argparse、legacy flags/alias/global options完整性待提取；MCP runtime wire schema亦未验证。

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
| GAP-11 | test_m1_route_matrix | 常量243，函数/docstring遗留239 | C后续改测试文案，本轮不改 |
| GAP-12 | scripts | check_ci_gates/check_skip_rate不存在 | E完整报告/selector门禁待建 |
| GAP-13 | v6 BUILD_ENV | 历史wiimu/Zig路径与当前环境混用 | E按profile核构建来源 |
| GAP-14 | mcp_full_schema.json vs源码/矩阵 | 两个环检测工具名已陈旧；总数相等掩盖集合漂移 | A重新提取真实schema、逐字段核验，不改当前上游源 |
| GAP-15 | task_collab_lifecycle.rs:1516–1524、task_collab_tests_core.rs:885–920 vs role-protocol.md:192–196 | returned同事务整改的源码/静态测试合同与协议“不自动追加”冲突，运行结果未知 | A解决权威语义冲突；D按冻结合同真实验证projection/steps/assignment、provenance、幂等与冲突 |

不再将“CI执行量0/全ERROR”称本轮实测：配置可推断前置风险，但部分静态测试不依赖daemon，具体数量需CI/JUnit。历史T2 157/72/0/14、T3 70/114/18/31只是旧叙述，非当前revision证据。

## 4. 待建运行审计

冻结expected selectors/profile/corpus/版本。报告collected、passed、failed、error、setup/call/teardown skip、xfail/xpass、collection模块skip、not_applicable、missing、UNKNOWN和中止。参数化按nodeid，模块importorskip不虚构未收集数量。

适用运行skip率S/N来自同次适用collected nodeid，collection skip另列且缺必需suite阻断；跨suite去重，平台分母事前冻结。零必需unexpected skip/xfail不受总比例豁免。

逐case报告连接入口/参数义务/selector/证据；JUnit不代替语义覆盖。空套件、缺报告、命令失败无JUnit、testsuites子节点漏聚合、maxfail截断、过期/版本不符/脏变更、selector缺失、UNKNOWN均拒绝放行。

## 5. 评审与产品边界

独立Agent PASS仅说明方案静态边界可继续规划，非产品通过、非daemon verdict。产品必须满足TESTING_PLAN §8。REVIEW记录本次文档评审；A–E实施/runtime清单/CI实测未完成，不存在“P0完成就完整交付”。
