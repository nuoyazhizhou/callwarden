# known-red 隔离机制（登记表 + quarantine job）

本目录是 callwarden 的 **known-red（已知红）注册表**：把「根因已定性、修复方案已裁决、
但尚未落地」的既有失败显式登记，使主 CI 的红**只表示本次改动引入的回归**，
既有红在单独的 quarantine job 里如实报数，并带**到期升级**约束——没有登记、
没有日落的红不允许存在（与 RETIRED_LEGACY_FILES「只减不加」同族的反豁免纪律）。

- 机器可读注册表：`known_red.json`（本文件说明其字段语义与流程）
- 注册表工具：`cicd/known_red.py`（校验 / 到期 / `--deselect` / `--quarantine-list`）
- 单测：`tests/test_known_red_registry.py`（schema 负向、到期负向、deselect 正
  向、登记漂移防护）
- CI 接线：`.github/workflows/ci.yml` 主 test job 的 deselect 注入步 +
  `known-red-quarantine` job

背景与三条目（KR-2026-10-23-A/B/C）的完整归因证据链见
`docs/testing/ci_malformed_bisect_evidence.md` §14.6 与
`docs/testing/full_stack_review_20261024.md` §3。

---

## 1. 字段语义（known_red.json）

| 字段 | 类型 | 语义 |
|---|---|---|
| `id` | string | 全局唯一编号，格式 `KR-<日期>-<字母>`（日期=红首次观测日） |
| `kind` | `"test"` \| `"workflow"` | `test`=pytest nodeid 级（可 deselect / 可进 quarantine 跑）；`workflow`=CI 门禁级红（只做到期跟踪，**不进 quarantine 跑**，因为它不是 pytest nodeid） |
| `nodeids` | string[] | `kind=test` 时必填、非空：登记的 pytest nodeid 全限定名（`path::test_name`）；`kind=workflow` 时为空数组 |
| `root_cause` | string | 一句话根因（可含文件:行号证据指针） |
| `owner` | string | 责任人（归因/裁决/修复实施者） |
| `closure_criterion` | string | 关闭条件（可验证的闭环判据；**不得**以放宽断言作为关闭手段） |
| `registered_date` | string(YYYY-MM-DD) | 登记日期 |
| `expiry_date` | string(YYYY-MM-DD) | 到期日（日落条款） |
| `status` | `"open"` \| `"closed"` | open=仍红/仍豁免；closed=已闭环（**保留条目不删除**，作为历史账本） |

顶层信封：`schema_version`（整数，当前 1）+ `entries`（条目数组）。

## 2. 登记流程

1. 红的根因已定性、修复方案已裁决但未落地 ⇒ 分配 `KR-<today>-<下一个字母>`；
2. nodeid **以本机 collect 为准**（`pytest <files> --collect-only`），禁手抄——
   手抄漂移由 `tests/test_known_red_registry.py` 的「登记漂移防护」用例兜底；
3. 填齐全部字段（缺任一字段 ⇒ `cicd/known_red.py` exit 64，CI 阻断）；
4. `expiry_date` 必填：`kind=test` 默认登记日 +30 天；`kind=workflow` 按修复
   紧迫性取短周期（KR-C 为 +7 天，因其修复已在实施中）；
5. 同目录 README 无需改（字段表即契约）；登记即生效于下一轮 CI（主 job
   deselect + quarantine job）。

## 3. 关闭流程

1. 按 `closure_criterion` 落地修复并取证（本机 + CI 形态）；
2. **MASKING 自检**：机制类修复必须证明「能再红」（如 KR-A 注入一行
   `sqlite3.connect` 后 audit 必须再 exit 1）——修完永远绿的门禁是假门禁；
3. 把该条目 `status` 改 `closed`（条目保留，作为历史账本；不得删除）；
4. nodeid 从 deselect/quarantine 集合自动移除（工具只认 `status=open`）；
5. 若到期仍未关：**不自动续期**。`cicd/known_red.py` 到期校验 exit 2 ⇒
   quarantine job 红升级 ⇒ 必须显式决策（加速修复 / 重估根因 / 修订到期日
   并说明理由），修订本身是一次有记录的治理动作。

## 4. quarantine 规则（为什么这样设计）

- **主门禁只表示「本次改动引入的回归」**：主 test job 用
  `cicd/known_red.py --deselect` 把 kind=test 且 status=open 的 nodeid 全部
  deselect。既有红不再淹没主门禁，主 job 绿 ⇒ 本次改动零回归；主 job 红 ⇒
  必为本轮引入（或 quarantine 之外的新漂移），信号恢复意义。
- **quarantine job 如实报数，KR 未修期间允许红**：`known-red-quarantine` job
  单独跑被 deselect 的 nodeid（不加 `-n auto`，单进程排除 xdist 干扰）。
  它红=KR 仍未修（预期状态，这正是隔离的目的——把「已知」与「未知」分开）；
  它绿=KR 已实际修复 ⇒ 必须立刻走关闭流程（否则构成「修了不关」的假账）。
  因此该 job 的结论**不设 continue-on-error**：红就红，但不阻塞主门禁结论，
  也不被容忍成永久背景噪声（到期校验会把它升级）。
- **到期升级**：quarantine job 末尾跑 `cicd/known_red.py`（无参=校验+到期）。
  到期未关 ⇒ exit 2 ⇒ 该 job 在「用例红」之外再添一个明确的
  「条目过期」失败，逼迫治理决策。
- **kind=workflow 条目只跟踪、不进 quarantine 跑**：它不是 pytest nodeid，
  没有可跑的用例实体；其修复状态由所属 workflow（如 acceptance-gates
  nightly）自身绿/红体现，本机制只对其做到期校验与升级。
- **deselect 与 maxfail 的关系**：deselect 注入的是 pytest 命令行参数
  （`--deselect <nodeid>`），与 `-n auto`、`--maxfail` 正交兼容。

## 5. 工具用法（cicd/known_red.py）

```
python cicd/known_red.py                      # 校验 schema + 到期检查（CI 门禁形态）
python cicd/known_red.py --deselect           # 输出 kind=test&open 的全部 nodeid 的 --deselect 串
python cicd/known_red.py --quarantine-list    # 输出待跑 nodeid（每行一个）
python cicd/known_red.py --registry <path>    # 指定注册表路径（默认 docs/testing/known_red/known_red.json）
```

退出码：`0`=OK；`2`=到期未关（列出过期条目）；`64`=schema/用法错误
（缺字段、类型错、kind/status 非法、日期非法、kind=test 缺 nodeid 等）。
