# preq — 契约附录（contracts appendix，append-only）

- 用途: 记录批次 1 评审发现的契约措辞/基线偏差的**决策依据与权威口径**。本文件是 `contracts.md` / `ac-map.md` / `requirements-brief.md` 的附录，**不改写三者的历史行**；口径冲突时以本文件「权威口径」为准（经主理人批次落定裁定，2026-11-28）。
- 覆盖 findings: F-2 / F-3 / T2-05 / T1-01 / T7-03（五条，源：findings-ledger.md）＋附带备注 T7-02。
- 交付基线: `run/preq-t6-integration @ 78d7ddc`（含 T1–T7 完整交付）。
- 性质: 设计工件（R5 批次落定产物），零生产代码。

---

## 附录 A — 卡2 聚合投影口径（承接 F-2 + F-3）

- **原契约措辞**（contracts.md 卡 2 GWT 要点 2）: 「capabilities 列表含该 projection」。
- **权威口径**: dispatch `handle_health` 聚合投影以**键值映射**透传域模块 capability 投影——键名 `prerequisites`（对象，三键 `name` / `schema_version` / `enabled`），**非 JSON 数组「列表」**。与 RP-05 既有 `role_prompt_compiler` 键值透传模式一致。
- **决策依据（F-2，🟡 contract-wording）**: 「列表」为泛指聚合投影的措辞，非数组要求；实现与 RP-05 既有模式一致，属契约措辞非实现偏差。本附录将措辞对齐为「键值映射透传」。
- **决策依据（F-3，💭 nit）**: 键名 `prerequisites` 为契约未指定的自由裁量；选定依据 = 与 T2 next_action 投影字段、T4 claim 门禁命名语义对齐（同一前置条件域）。**自本附录起键名固化为 `prerequisites`**，后续实现（含批次 2）不得另取名。
- 影响面: `dispatch.rs::handle_health` 出口；不涉及 claim/投影判定语义。

---

## 附录 B — 卡3 binding 缺失口径（承接 T2-05）

- **原契约措辞**（contracts.md 卡 3 异常路径）: 「task 无 binding/workspace 无法解析 → 沿用既有 blocking_reasons 路径，prerequisites 置 []」。
- **基线实际**: 无 binding/策略错误 → 沿既有路径返回 **Err `E_WORKSPACE_AUTHORITY_UNAVAILABLE`**（fail-closed），并非 blocked + `[]`。
- **权威口径**: **依赖不可评估 ≠ 依赖未满足**——binding 缺失属基础设施/授权失败，按 fail-closed 抛错处理，不静默置 `[]`（静默置 `[]` 会被消费方误读为「无未满足依赖」而放行）。
- **决策依据**: 原句两支（措辞与实现）均无法字面满足；fail-closed 与全局异常矩阵 #5（依赖失败 → 向上抛、不静默放行）一致，故以实现口径为准并在此固化。T11 第三项测试即锁定本口径。

---

## 附录 C — 卡1 数据源口径（承接 T1-01）

- **原契约措辞**（contracts.md 卡 1 数据源）: 「`dependency_edges WHERE workspace_id=? AND consumer_task_id=? AND is_hard=1`」。
- **实现口径**: 判定引擎读 **`task_dependencies`**（声明表，`is_informational==0` 视为 hard）＋ `tasks.status` ＋ `artifact_identities.freshness_status` ＋ `interface_provider_selections`。
- **决策依据**: `dependency_edges` 是**物化边表**，仅物化 artifact/interface 类硬边且 `is_hard` 恒 1——`requires_existing` / 未知 dep_type / 信息性边在该表**不可表示**；按 dependency_edges 过滤会漏掉 requires_existing 类判定。实现取声明表为准**正确**，偏差已文档化（prerequisites.rs docstring）。
- **固化口径**: 判定以 `task_dependencies` 声明行为主数据源，`is_informational==0` 为硬依赖；`dependency_edges` 仅作 freshness/物化辅助查询。T9 附录即本条的历史替代（本文件）。

---

## 附录 D — 词汇对照（承接 T7-03）

- 「is_hard=0」（契约措辞）↔ `is_informational=0`（schema 列名）: 同一语义（信息性边），沿自契约卡 1 原文，语义无歧义。
- **固化口径**: 批次 2 起文档统一使用列名 `is_informational`；「is_hard」仅在与 schema.py 注释直接对话时保留。

---

## 附带备注（T7-02，未立单）

- §8.6 异常编号与 brief §4.3 八场景矩阵存在重排错位风险——下次文档维护时在各场景加「brief §4.3 #N」原编号注即可，无需专门单。

---

## 处置索引（与 findings-ledger.md 对账）

| finding | 处置 |
|---|---|
| F-2 / F-3 | 本附录 A（无单，主理人裁定直接产出工件） |
| T2-05 | 本附录 B |
| T1-01 | 本附录 C |
| T7-03 | 本附录 D |
| T7-02 | 本文件「附带备注」 |
