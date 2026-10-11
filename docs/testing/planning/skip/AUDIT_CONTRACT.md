# TP-03 Skip 逐站点审查与运行映射合同

- **任务**：`T-1791627785150-67976028`（TP-03，skip 逐站点审查与运行映射合同）
- **单一 owner**：skip-audit
- **批准方案**：`docs/testing/TASK_BREAKDOWN.md`（TP-03 段）
- **基线**：`docs/testing/registration/source_baseline.json`
- **本文件位置**：`docs/testing/planning/skip/AUDIT_CONTRACT.md`（allowed 白名单内）

## 1. 四步流程与状态

| 步 | step_id | target | 交付 | 状态 |
|---|---|---|---|---|
| 0 | `S-1791627785151-67b305e4` | `site_reviews.json` | 468 站点审查清单 + 源码 hash 绑定 + 取证 | done |
| 1 | `S-1791627785152-67ba2f7c` | `site_reviews.json` | 逐站点静态根因判定（前置/平台/缺陷/历史机制） | done |
| 2 | `S-1791627785152-67ba8c10` | `runtime_mapping.schema.json` 等 | 冻结 nodeid/collection 与运行关联规则 | 进行中 |
| 3 | `S-1791627785152-67bab190` | `validate_skip.py` | 验证器（`--self-check` / `--verify-reviews`）+ 人工审查报告 | 待 |

## 2. allowed 白名单

所有 basename 以 `docs/testing/planning/skip/` 为根：

- `site_reviews.json`
- `runtime_mapping.schema.json`
- `validate_skip.py`
- `AUDIT_CONTRACT.md`
- 另：`deliverables/testing/TP-03/`（证据目录，本轮不创建）

**禁止**：不改已有 tests 中的 skip；不编辑未列 allowed 的源码路径；不碰生产业务库/manifest/lease/CAS；不运行产品、不构建、不联网、不写生产源仓数据。

## 3. 站点口径（与卡片数字对拍）

- 全部 **468** 站点，site_id 唯一（468/468），跨 146 文件
- classic（skip + skipif）= **447**；importorskip = **21**（模块前置单列）
- reason 语法分类：字面量 **282** / 表达式 **167** / 缺省 **19**（只语法，不承担运行语义）
- 每站点 `file_sha256` 与磁盘对拍通过（468/468）

## 4. 静态根因判定（Step 1 结果）

- **468/468 classified，0 UNKNOWN/UNREVIEWED**；对 Step 0 首轮 326 条已判定站点零回退
- 源码窗口 sha256 校验 468/468 通过
- 证据来源：condition 363 / source_window 49 / guard_definition 35 / condition_or_module 21
- 判定算法：Phase A 只认条件/守卫定义（首轮幂等），Phase B 用跳过行严格上方 60 行源码窗口补证据；文档行与代码行分离，代码行最近距离匹配
- 族：PREREQ_BUILD 245 / PLATFORM_OS 52 / PREREQ_FIXTURE_DATA 46 / PREREQ_DAEMON 31 / PREREQ_EXTERNAL_TOOL 30 / PREREQ_MODULE 26 / PLATFORM_CAPABILITY 12 / PREREQ_RUNTIME_DATA 10 / PREREQ_ENVFLAG 8 / HISTORICAL_LEGACY 5 / PLATFORM_PYVERSION 2 / PREREQ_LLM 1
- 新增子族：`PREREQ_DAEMON`（守护进程运行时前置）、`PREREQ_RUNTIME_DATA`（live daemon 数据/测试数据表前置）
- 全部条目状态 = `static_classified_pending_human_confirm`；`review_complete=false`；`human_review.state=pending`（confirmed=0）

### 4.1 族定义

| 族 | 语义 | 是否缺陷 |
|---|---|---|
| prerequisite | 前置不满足（构建产物/模块/外部工具/数据/环境开关/daemon 运行时） | 否 |
| platform | 平台限定（OS/内核能力/Python 版本） | 否 |
| defect | 已知缺陷绕过（源码注释/issue 明确） | 是（本账本未检出） |
| historical_mechanism | 历史机制约定（frozen build / RPC 契约容错降级） | 否 |

## 5. 运行关联规则（Step 2 冻结）

见 `runtime_mapping.schema.json`（`runtime-mapping/v1`）。要点：

- **只冻规则，不产运行关联**：`runtime_association.declared=false`、`expected_only=true`
- **nodeid 权威来源** = 真实 pytest 运行（--collect-only / 运行报告）；禁止静态反推冒充
- **collection 权威来源** = 真实运行的收集全集；每条记录须绑定运行证据
- **静态期望 vs 运行结果分离**：`expected_nodeids`（Step 2 产出）不是运行结果，不得被复制为 observed 记录
- **position_kind 七类**（机械可复算）：statement_in_test 155 / statement_in_fixture 64 / decorator 55 / decorator_class 116 / module_pytestmark 36 / module_importorskip 15 / marker_variable 24 / marker_variable_dead 3
- **runtime reason**：未触发记 `NOT_TRIGGERED` 占位，不得填写 reason 文本；源码 reason 字面量不是运行 reason

### 5.1 静态/运行分离原则

- 447 classic 站点是**源码机制出现点**，不是运行 skip 结果：只有在真实运行中触发才成为运行 skip 记录
- 21 importorskip 站点为**模块前置**：模块可导入时整模块 test 正常运行（站点 NOT_TRIGGERED）
- `static_root_cause` 回答「静态根因族/规则」；运行关联回答「真实运行是否触发、影响哪些 nodeid」——两者不可互相冒充

## 6. 验收（Step 3 实现后运行）

```
C:/Python314/python.exe docs/testing/planning/skip/validate_skip.py --self-check
C:/Python314/python.exe docs/testing/planning/skip/validate_skip.py --verify-reviews
```

- `--self-check`：账本自洽（站点唯一、hash 匹配、schema 枚举合法、position_kind 覆盖完整）
- `--verify-reviews`：逐站点复核（每条人工分类有源码理由与 owner；未完成为 UNREVIEWED 阻断审查完成宣称）

## 7. 负向（一律拒绝）

1. 遗漏/重复站点
2. 以 reason 字面量冒充根因
3. 未触发捏造运行 reason
4. collection 虚构 nodeid
5. 删除必需 suite 逃逸
6. 把 447 classic 站点当运行 skip
7. 把静态规划 PASS 表述为产品运行 PASS
8. source 变更后仅沿用行号（不重新审查）
9. 缺前置/空套件/UNKNOWN/非法副作用/越界连接/证据陈旧

## 8. 源码变更规则

- site 的 `file_sha256` 变化 = source 变化 = 必须重新审查（禁止仅沿用行号）
- `SKIP_SITES.json` / `gen_test_cases.py` 的 sha256 与 registration 基线不一致时，账本作废重算

## 9. 下游

- **TP-10/11**：产出真实运行关联（collection 记录、observed nodeid、runtime reason），与本合同 mapping_rules 对齐
- **TP-13**：核验必需 skip=0 及 NOT_TRIGGERED 合法依据

## 10. 回滚

只撤回本卡已归属的 commit/白名单文件；不 reset 共享工作树、不删旧证据、不改生产数据。
