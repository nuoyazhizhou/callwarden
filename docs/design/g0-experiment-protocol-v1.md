# G0 Experiment Protocol v1

## 目的和范围

本规格是 Call Warden G0 盲评实验的唯一协议真相源。它约束 Recovery Agent、Batch Creator、Independent Reviewer 和 Coordinator 的职责、证据目录、状态机、配对规则、记录格式和 fail-closed 行为。

G0 只产生 `non_product_evidence`。G0 通过不能自动开启 P1，也不能替代产品验收、代码审查或发布门禁。

## 角色边界

| 角色 | 可以做 | 禁止做 |
|---|---|---|
| Recovery Agent | 恢复可信 DB/Git provenance，生成只读证据报告 | 创建 batch、写 Reviewer 记录、修改历史证据 |
| Batch Creator | 构建候选池、配对、冻结 batch、交付 Reviewer home | 写 TP/FP/misses、duration、verdict、record-*、apply/close |
| Independent Reviewer | 验证交接、盲评样本、写 review records、生成 final report | 修改源码、协议、阈值、manifest、历史批次 |
| Coordinator | 选择下一阶段、检查任务状态、委派角色 | 代替角色补证据或绕过门禁 |

每个角色使用独立 session、独立 agent identity 和独立工作目录。后一个角色只能消费前一个角色的只读交付物。

## 状态机

```text
created
  -> recovery_pending
  -> recovery_passed
  -> creator_preflight_passed
  -> batch_frozen
  -> reviewer_running
  -> final_evidence_complete
  -> eligible_for_p1
```

任一阶段失败进入不可自动前进的状态：

```text
recovery_failed
creator_preflight_failed
paused_disclosure
invalid_evidence
review_not_eligible
```

失败状态只能通过新的、独立且可验证的证据进入后继阶段；不得覆盖失败报告或重写历史 JSONL。

## 证据等级

| 类型 | 含义 | 可否证明当前整改 |
|---|---|---|
| `source_implementation` | 任务归因明确的源码/测试实际变更，含 step、path、hash、非空 diff | 可以 |
| `historical_provenance` | 可解析的原始 commit、父提交、tree 和文件 hash | 只能证明历史来源 |
| `evidence_repair` | 事后补录的当前文件归因 | 不能声称原始实现时已有证据 |
| `historical_provenance_snapshot` | 旧提交的追溯快照 | 不能替代当前 change set |
| `reviewer_observation` | Reviewer 评审中的观察和 verdict | 不能作为 Creator 预填缺陷 |

`evidence_repair` 和 `historical_provenance_snapshot` 必须显式标记，不能与原始实现证据混称。

## 证据目录和不可变性

每个 batch 必须有独立的 Creator home 和 Reviewer home。Reviewer home 必须是真实目录，不能是 junction、symlink 或 scratch 的别名。

Reviewer home 至少包含：

```text
.callwarden/experiments/<BATCH_ID>.jsonl
batch_config.json
manifest_<BATCH_ID>.json
blind_package_<BATCH_ID>/
blind_package_manifest_<BATCH_ID>.json
evidence_manifest_<BATCH_ID>.json
report_<BATCH_ID>.json
REVIEWER_HANDOFF.md
```

Evidence manifest 必须绑定绝对路径、大小、记录数和 SHA-256。交接前 JSONL 只能包含当前 batch 的 `pre_verdict` `blind_view`；不得包含 metrics、verdict、reveal、invalid 或 incident。

历史批次的 JSONL、report、manifest、incident、invalid、阈值、协议指纹和任务状态属于只读证据。发现路径缺失或 hash 不匹配时立即失败，不得从默认 home、Creator home 或其它副本推断。

## Creator 前置门槛

冻结 batch 前必须同时满足：

- 候选池至少 48 个；
- 独立缺陷证据至少 12 个；
- paired 后 Control 至少 6 个缺陷证据候选；
- paired 后 Treatment 至少 6 个缺陷证据候选；
- 真实非空 diff、任务/步骤归因和 scope contract 齐全；
- 无历史 Reviewer 结论、污染或估算 token；
- 16 个完整 pair，Control/Treatment 各 16 个。

配对要求：完整 `strata_key` 相同、`pair_id` 唯一、slot 0/1 各一个、两组方向相反。候选缺陷证据只能用于分层，不能预填 TP/FP/misses 或 verdict。

## Reviewer 记录规则

Control：盲视图 -> 独立判断 -> `record-metrics`。

Treatment：盲视图 -> 首轮 verdict 封存 -> reveal -> 如有变化记录变化 -> `record-metrics`。

所有 duration 必须是真实测量；`nontrivial` 必须由系统从 diff/symbol evidence 自动判定；不得手填。发现盲法污染或证据不完整时记录 incident/invalid，并按协议暂停，不能继续补分母。

Final report 必须区分 `recall_defined=false` 与 recall=0，并保留样本不足、污染、延迟和误报的原始分母。

## Fail-closed 总则

以下任一条件成立即停止：来源 DB 不可信、Git hash 不可解析、scope required path 缺失、diff 为空、证据路径未绑定、JSONL 混入其它 batch、Treatment 泄露 notes/verdict、候选门槛不足、Reviewer home 不是独立目录、历史 hash 改变、命令退出码非零或角色越权。

停止时只写当前阶段报告和日志，不创建后继 batch，不写 Reviewer record，不执行 task apply/close，不降低阈值。
