# T-1795469778065-1686c8a0 裁决证据（webobs-adjudication）

- 任务：T-1795469778065-1686c8a0（Web 观测面 MVP：本机只读项目进展看板）
- 裁决身份：agent_id=adjudicator-webobs-01 / agent_instance_id=inst-webobs-adjudication-20261129-01 /
  session_id=sess-webobs-adjudication-20261129-01 / model_id=custom-local:Sage-S2.1-Flash / role=adjudicator
- 日期：2026-11-29 · 工作区：C:\git_work\callwarden（workspace_id=1 / instance=4baea3ff12c2ea5c）
- 复评 verdict：V-89a5c56f5d1e2ce45308f0ab（event 941，phase=blind_first_pass，overall=pass，findings=8）
- daemon 权威路由：next-action=adjudicate_current_verdict，review.state=passed，decision=READY/ADJUDICATE

## 1. 三前提核验（全部通过）

### 1a. verdict 落账核验 —— PASS

只读查询 daemon 主库（`file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro`）：

```sql
SELECT id, verdict_id, task_id, contract_id, contract_revision, phase, overall,
       reviewer_identity, step_id, role_contract_lineage_id, role_contract_revision_id,
       role_contract_revision, submitted_at, workspace_id
FROM task_verdict_events WHERE id=941;
```

输出（关键字段）：

- verdict_id=V-89a5c56f5d1e2ce45308f0ab，task_id=T-1795469778065-1686c8a0，
  contract_id=TC-T-1795469778065-1686c8a0（revision=1，hash sha256:71d35e5b…，经
  task_contract_revisions 表复核存在且绑定本任务），phase=blind_first_pass，**overall=pass**。
- step_id=S-5965534c3f1d8d37fa31ed59，经 task_steps 表复核：该 step 属本任务、action=fix_defect、
  status=done、result 内嵌 source_findings 全文（首轮 V-a61eddec9a559e14ef08da96 的 F-1…F-6）——step 绑定完整。
- role_contract：lineage rcl-T-1795469778065-1686c8a0-reviewer，revision_id
  rcr-T-1795469778065-1686c8a0-reviewer-r1（revision=1，hash sha256:3e8debc9…，与 reviewer_identity
  内嵌 role_contract.hash 一致，经 role_contract_revisions 表复核存在）。
- reviewer_identity 五元组：agent_id=**reviewer-webobs-r2** /
  agent_instance_id=**inst-webobs-rereview-20261129-01**（非空，独立 reviewer）/
  session_id=**sess-webobs-rereview-20261129-01** / model_id=**Sage-S2.1-Flash** / role=reviewer
  ——与本单 handoff 所载 reviewer 身份逐字段一致。
- findings=8 条（F-1/F-2/F-4/F-5 修复 pass；F-3/F-6 披露偏差转 adjudicator；REC-1/REC-2 记录级）。

### 1b. lease/fencing 溯源核验 —— PASS

```sql
SELECT lease_id, task_id, role, agent_id, session_id, model_id, fencing_counter, status,
       acquired_at, released_at, token_hash FROM task_leases WHERE lease_id='L-bb6d798442e55b8c';
SELECT event_id, event_type, fencing_counter, actor_agent_id, actor_session_id, actor_model_id
FROM task_lease_events WHERE lease_id='L-bb6d798442e55b8c' ORDER BY id;
```

- lease L-bb6d798442e55b8c：task=T-1795469778065-1686c8a0，role=reviewer，
  holder=reviewer-webobs-r2 / sess-webobs-rereview-20261129-01 / Sage-S2.1-Flash（与 verdict reviewer
  身份一致），**fencing_counter=2**，status=released（正常释放），token_hash=dbd0c7a6…（派生凭据在账）。
- lease 事件：EVT-e9deb464205a16aa（acquire，counter=2）→ EVT-43171c47bc3e1ebc（release，counter=2）。
- 时间线绑定：verdict submitted_at=1795869555.0 落在 lease 活跃窗口
  [acquired_at=1795868805.0, released_at=1795870061.0] 之内。
- 机制核验（源码）：daemon verdict.submit 为受保护写，强制 `require_lease_params` +
  `validate_lease_for_mutation`（rust_ext/src/daemon/task_collab_verdict.rs:115,143-150）——
  token hash / fencing counter / holder identity 三重匹配 fail-closed
  （task_collab_lifecycle_ops.rs:238-276）。verdict event 941 在账即证明写入时 lease 校验通过。

### 1c. handoff 证据核验 —— PASS

```sql
SELECT event_id, task_id, from_status, to_status, reason_code, agent_session_id,
       evidence_path, evidence_hash, request_id, step_id, role
FROM task_events WHERE event_id=18512;
```

- event 18512：reason_code=handoff_structured，review→review，session=sess-webobs-rereview-20261129-01，
  role=reviewer，step=S-5965534c3f1d8d37fa31ed59，request_id=ho-T-1795469778065-1686c8a0-rev-2，
  evidence_path=evidence/T-1795469778065-1686c8a0/webobs-rereview-evidence-20261129.md，
  evidence_hash=c3e5be97f3f7dd74565500986418edce2e67491f83ff256e49083ca090fe5bd9。
- 本机实算：`sha256sum evidence/T-1795469778065-1686c8a0/webobs-rereview-evidence-20261129.md`
  → `c3e5be97f3f7dd74565500986418edce2e67491f83ff256e49083ca090fe5bd9` ——**逐字节一致**。

## 2. F-3 / F-6 裁决

### 2a. F-3（pyproject package-data 未实施）—— 裁决：登记为已知缺口，建后续单，不阻塞本卡

事实（本日核验）：

- pyproject.toml:147-150 `[tool.setuptools.package-data]` 仍仅 `i18n/*.json`，静态目录
  （server/web_static/index.html）未加入；本卡技术约束明确要求「静态目录加入 pyproject 的 package-data」。
- docs/design/web-observer-read-model.md §7 验收对照表（:126）第 6 行仍声称
  「静态目录入 pyproject package-data」，与 pyproject.toml 事实矛盾（文档内部矛盾未消）。
- 整改 step 白名单仅 3 文件（server/web_gateway.py、server/web_static/index.html、
  docs/design/web-observer-read-model.md），pyproject.toml 不在其中；executor 已如实披露偏差
  （首轮证据「已知限制」节 + 复评核项 g.3 状态属实），非隐瞒。

裁量理由：

1. **影响面**：editable 安装运行时零影响（静态文件直接从仓库服务）；仅 wheel 打包会缺
   server/web_static/index.html。属打包完整性缺口，非功能/安全缺陷。
2. **成本不匹配**：修复为 pyproject.toml +1 行、read-model §7 更正 1 行；若重开整改回路
   （新 fix_defect → claim → report → 独立复评 → 重裁决），治理成本与两行改动严重不匹配。
3. **先例**：ADJ-EVIDAPP（evidence/T-1795835014164-3e755900/）——adjudicator 裁定 adjacent
   问题立独立单、不阻塞主线。

**裁决**：F-3 登记为已知缺口（known gap），不阻塞本卡 ACCEPT；建 adjacent 后续单承接修复。

**去向**：后续单 **T-1795872106380-72d3c4ec**（event 18519 创建，「webobs F-3 后续：server/web_static
加入 pyproject package-data 并更正 read-model §7」），step S-1795872106385-73263a4c（write，
target_file=pyproject.toml,docs/design/web-observer-read-model.md，按 F4-裁A 先例逗号分隔申报全部
涉及文件），三角色契约齐备（executor/reviewer/adjudicator），daemon 路由 READY/CLAIM。
后续单验收：① [tool.setuptools.package-data] 增加静态目录条目；② read-model §7 表述更正与事实一致；
③ 最小行为级验证 package-data 声明生效。

### 2b. F-6（--token 仅参数位）—— 裁决：无需动作（知情设计偏差，非缺陷）

事实（本日核验）：

- server/web_gateway.py:401-424：--token 仅在非 loopback 启动时做存在性检查，无实际校验；
  启动与页面均有显著警告。
- docs/design/web-observer-read-model.md §6「明确不纳入本卡（后续单独建卡）」（:106-113）明载：
  「局域网证书、登录认证、多用户权限 | 本机 HTTP（localhost 为安全上下文）即可验证；
  `--host 0.0.0.0` 仅保留 `--token` 参数位并要求显式传入，完整认证后续卡」。
- 任务卡描述同样声明：「仅显式 --host 0.0.0.0 才允许对外，并保留 token 参数位（本卡不实现完整认证）」，
  且「明确不在本卡范围（后续单独建卡）：……局域网正式证书与登录认证」。

**裁决**：F-6 无需动作。--token 参数位系按设计为后续认证卡预留的接口位，属知情偏差而非缺陷；
executor 已实施启动/页面显著警告作为暴露前提示。跟踪去向：任务卡「明确不在本卡范围」节已声明
认证后续单独建卡，本裁决不另建单（避免重复建卡）。

## 3. 受保护收尾（apply → close → COMPLETE）

按 adjudicator 角色契约（cw.aprime.adjudicator.startup.v1）：ACCEPT → 有效 reviewer lease/fencing
→ task.apply → task.close → next_action=COMPLETE。

1. **身份注册**：agent.register（daemon 权威 RPC）——adjudicator-webobs-01 /
   inst-webobs-adjudication-20261129-01 / sess-webobs-adjudication-20261129-01 /
   custom-local:Sage-S2.1-Flash / role=adjudicator → status=registered（独立身份，与 executor
   executor-webobs-mvp-01、reviewer reviewer-webobs-r2 无身份交集）。
2. **lease 获取**：`cw lease acquire T-1795469778065-1686c8a0 --role reviewer`（adjudicator→reviewer
   为 daemon 项目化默认）→ lease_id=**L-cf47e40b856f0494**，**fencing_counter=3**（全历史 MAX+1），
   ttl=3600s。lease 事件：EVT-01c7dadb375f4dfb（acquire，counter=3）。
3. **task.apply**（review→applied）：携带完整身份 + lease token + fencing_counter=3 →
   ✓ Task applied，applied_at=1795872131.07。**事件 ID=18521**（task_events，role=adjudicator）；
   action_identities 7313 记录完整四元组+instance。
4. **task.close**（applied→closed）：同凭证 → ✓ Task closed，closed_at=1795872135.63。
   **事件 ID=18522**（task_events，role=adjudicator）；action_identities 7314 记录完整身份。
5. **lease 释放**：`cw lease release` → status=released，released_at=1795872143.0
   （EVT-95c646b3b48ec4e4，counter=3）。
6. **终态验证**：`cw task next-action T-1795469778065-1686c8a0` → lifecycle_status=**closed**，
   workflow_status=**completed**，decision=**COMPLETE**，next_action=finalize，blocking_conditions=[]。

## 4. 裁决结论

- 复评 verdict V-89a5c56f5d1e2ce45308f0ab（overall=pass）三前提核验全部通过，予以 **ACCEPT**。
- F-1/F-2/F-4/F-5：修复经独立复评 pass，本裁决维持。
- F-3：登记为已知缺口，后续单 T-1795872106380-72d3c4ec 承接，不阻塞本卡。
- F-6：知情设计偏差，无需动作，记录在案（认证后续卡承接）。
- REC-1/REC-2：记录级事项，维持复评结论（F4-裁A 替代追溯链可接受；8590c73 内容完整落库不回滚历史）。
- 收尾：apply 事件 18521、close 事件 18522、终态 COMPLETE 已验证。本裁决未修改任何源码文件，
  仅新增本证据文档与后续任务卡（daemon 权威写点）。
