# Role Prompt Compiler v2 议程登记

> 本文件是 v2 议程的权威登记点（冻结 spec `cw-role-prompt-compiler-v1-frozen-spec.md`
> byte-frozen，受 build gate hash 校验，不得修改）。v2 议程项在此登记后，进入
> successor capability 派工流程（对齐 v1 spec §19 的依赖顺序与治理门禁）。

## A-1（P1-CR5 终态）：blocked_recovery 角色路由推导

- **来源**：`docs/reports/client_convergence_codereview_20260909.md` P1-CR5
  （第一阶段 behavior 复现：`cw.py task prompt T-1788313854785-dd64cebc` →
  `prompt_kind: blocked_recovery`、`Required role: —`、`routing state: non_actionable`；
  第三阶段重定性：BLOCKED 不派角色是 v1 spec §6 的范围限制，非实现缺陷）。
- **问题**：决策为 BLOCKED 时 prompt 不给出"哪个角色来处理恢复"，四角色
  无人值守循环在 blocked 任务上无从自动认领。
- **v2 目标**：`blocked_recovery` 分支按 `blocking_reasons` 推导恢复角色
  （至少输出"建议路由"），映射示例：
  - `governance_blocked` / contract 缺陷 → planner（post-cutover 原生承接）
  - identity / lease / 门禁缺口 → adjudicator（lease.recover 类恢复）
  - 实现缺陷回归 → executor
  - 跨角色 / 权限外 → 保持 user route（spec §20-4 纪律：BLOCKED 只指向
    内部恢复，不伪造 decision authority——推导出的角色仍须过真实
    claim/lease 门禁，prompt 不构成授权票据）。
- **依赖**：`blocking_reasons` 枚举在 v1 route 面已结构化（route.rs），
  无新 schema；需与 `planner_governance_v1`（spec §19-2）合并派工，避免
  blocked → planner 路由在 planner capability 未上线时产生死路由。
- **验收锚点**：含 blocking_reasons 的 blocked 任务编译出的 prompt 必须带
  非空 required role 或显式 `user_route` 理由；四角色循环在 blocked 任务上
  可自动认领或明确升级 user。

## A-2（随 CR4 落地归档）：写入侧 hash 规范化

- CR4 本体已于 2026-09-09 以 `normalize_bare_sha256_refs` 落地（daemon
  task 写入口自由文本统一 `sha256:` 前缀，commit `dea6f4a`），不再占用
  v2 议程。此处登记仅作 successor capability 追溯：若未来 evidence/
  payload_hash 结构化字段需要双形态归一化（spec §7.1-3 已定义
  normalize_sha256_form），须先消除裸字符串相等比较点（gate evidence
  匹配等）后方可改写存储格式。
