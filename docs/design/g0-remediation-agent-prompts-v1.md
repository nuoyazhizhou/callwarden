# G0 阳性与污染整改 Agent 提示词 v1

本文件对应父任务 `T-1786022666252-b49dbdc8` 的 5 个子任务。历史批次
`B-1786007826323-60a9ada9` 的 JSONL、report、manifest、incident 和 invalid
记录均为只读证据，任何 Agent 都不得回写、删除、重记或修改其指标。

## 1. 迁移计划与实际实现证据对齐

任务：`T-1786022678812-e2514303`

```text
你负责 G0 阳性整改 1。目标不是修改历史 G0 判定，而是修复“任务声称完成 6 个
P0/P1 修复，但可见证据主要是计划/说明文档”的纳样与交接缺口。

先读取任务 T-1785076592821-6b0d7555 的完整步骤、target_file、change_audit、
symbol_changes 和测试记录，逐项判断每个承诺是否有对应实现证据。不能只看任务状态
或 commit 数量，也不能把文档 diff 当成代码实现。

实现要求：不改历史 G0 JSONL/report/manifest，不伪造 TP/FP/misses；新批次必须使用
scope contract。profile=code_change 时必须命中实现路径并存在至少一个非文档 tracked
source file；required_paths 缺失即拒绝纳样。为缺失证据补真实实现和回归测试；无法补齐
时输出 remediation_required 并保持 review/open。运行针对性 pytest、py_compile，
不要执行 task apply/close。
```

## 2. Artifact inspector 任务范围漂移

任务：`T-1786022678812-bc935fc4`

```text
你负责 G0 阳性整改 2。目标是阻止“只要求 artifact inspector/跨平台验收，却混入
Rust/Python/CLI/DB 大规模改造”的范围漂移。

读取 T-1785024538957-b4bc9f03 的任务描述、步骤 target_file、change_audit 和提交关系，
列出 declared scope 与 actual changed paths 的差集。历史批次只读。为新任务生成
scope-contract JSON：required_paths 至少覆盖 inspector、workflow 和验收测试；
allowed_paths 只包含这些目录及必要文档。`cw experiment admit --scope-contract`
必须在写入 blind_view 前拒绝任何 outside path 或 required path 缺失。增加缺 required
path、outside path、合法 inspector-only 三类测试。运行 pytest/py_compile，保持 review，
不执行 apply/close。
```

## 3. 三份规格真相源同步缺口

任务：`T-1786022678812-6b270725`

```text
你负责 G0 阳性整改 3。目标是阻止“任务要求同步 requirements/design/tasks 三份真相源，
但实际只改了 AGENTS.md”的假完成。

读取 T-1785574343859-117469ed 的父任务、失败 verify 步骤、三个真相源的当前 hash、
change_audit 和 step_targets。required_paths 必须精确包含
docs/design/requirements.md、docs/design/multi-llm-contract-driven-collaboration-design.md、
docs/design/tasks.md；allowed_paths 不得允许 AGENTS.md 作为替代。增加三份文档一致性检查，
只改 AGENTS.md 必须失败，三份均同步才通过。缺失内容要真实补齐，否则保持 review/open。
运行文档检查、pytest/py_compile，不执行 apply/close。
```

## 4. P08 盲法污染与暂停治理

任务：`T-1786022678812-7d7d65ca`

```text
你负责 G0 整改 4。当前批次 P08 是一个 disclosure incident，导致两个样本 invalid；
不是两个可修复的 TP，不能删除或重写历史记录。保留 Treatment
T-1785024538955-1858a947 的 incident 和 pair 中两个 invalid_sample，报告保持 paused、
non-product、eligible_for_p1=false。

纳样前扫描 change_audit/step_targets/open findings 中高置信历史复审路径、既有 Reviewer
verdict 和明确 P0/P1 结果；发现即 fail-closed，不追加 JSONL。普通任务描述中的“修复 P0/P1”
不能单独触发，补正例/反例测试。最终 report 必须允许 review_started 后生成 final evidence
bundle，但 handoff bundle 仍只能包含 blind_view；历史短/长 incident type 都要归一化。
运行 focused suite，保持 review。
```

## 5. 新 paired G0 批次独立验收与 Reviewer 交接

任务：`T-1786022678812-7654a336`

```text
你负责 G0 整改 5。只有整改 1–4 经独立 Reviewer 通过后，才创建新的 paired_v3 批次；
当前批次不能续用，也不能通过重写报告开启 P1。

Creator 必须使用 batch-specific Creator/Reviewer home，禁止 junction/scratch 作为权威
目录；绑定 JSONL、manifest、blind-package、report、evidence manifest 的 hash；使用
paired_v2/v3 保证同 strata 的 pair slot 0/1；两组缺陷覆盖候选各至少 6 个；每个样本
纳样带 scope-contract；不调用任何 record-*，不执行 apply/close/report。

Independent Reviewer 只接受 Reviewer home 初始 JSONL，先校验路径/hash/pair/盲包，再逐样本
review。Treatment 先 verdict 后 reveal；duration 必须真实；token 使用 real 或明确
unavailable。最终用 `cw experiment report --artifacts-dir <JSONL_PARENT>` 生成 final
bundle。任何 incident/污染/证据不完整都保留原始记录并暂停或 invalid；只报告 G0
non-product 结论，不自动开启 P1。
```
