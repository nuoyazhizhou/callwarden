# A″-G0 → 独立 Reviewer 交接（executor_ready_for_independent_review）

- **task_id**: `T-1787800241077-e7fd7231`（A″-G0 [Gate/client_boundary]：PyO3 daemon/authority surface manifest、HTTP successor 与 retireability 冻结）
- **step**: `S-1787800317700-b1dfcdfc`（step_index 3，action=`prepare_independent_review`）
- **snapshot_id**: `02cf30ebfce924b0`
- **role contract (executor)**: `rcr-T-1787800241077-e7fd7231-executor-r1`（skill_version=`aprime2-client-boundary-g0-v1`）
- **本步 check_items**：记录 A′/matrix/runtime/S3 实施 release blockers；准备无秘密 evidence list；不创建 implementation microtask 或部署。

## 1. 交接结论

G0 四个 executor 步骤（step0 全量 inventory / step1 import+ABI audit / step2 HTTP successor+release map / step3 本交接）均已产出行权证据，全部为**只读静态冻结**：未修改任何 production source / runtime / task state / matrix；未建立任何 A″-NN implementation microtask；未执行部署/refresh/apply/close。请 Reviewer 按独立合同进行**独立复核**并提交 `reviewer_pass` / `reviewer_blocked` verdict。

## 2. 无秘密 evidence list（reviewer 可独立复现）

| # | Evidence 文件 | sha256（磁盘字节） | 证明 | 不证明 |
|---|---|---|---|---|
| E0 | `deliverables/software-company/aprime2/pyo3_surface_manifest_v1.json` | `2008c5ccc5236f0339dcc6ea38b1565db22a108021cea68016581e76457239b9` | step0 全量清单：162 个 `wrap_pyfunction!` export 逐项 category / source / registration / disposition / required_gate / HTTP_successor / retirement condition；34 candidate + 128 local-core | disposition 之后的 caller 现实（由 E2 补） |
| E1 | `deliverables/software-company/aprime2/pyo3_surface_manifest_v1.md` | `f781cd3c7d3753aa591af43301dceb8d92a69de9b9a2f14ff5d0775cbda1a302` | step0 inventory 人类可读版本（含 meta.source 范围声明） | 全库真实调用（E2 补） |
| E2 | `deliverables/software-company/aprime2/pyo3_import_use_audit.md` | `6e23a3aca7e66fbf952d5b20a7d3f5e1d97af32fb2b12d254bee427c11ad351b` | step1 独立 AST/import/attr/raw/dynamic 审计：159/162 有 consumer 记录；103 个 py 文件真实 AST import；dynamic/PyInit 4 处；AST 全 0 仅 3 export；**纠偏 inventory 0-hit 范围限制，不构成 retire 许可** | 外部世界 ABI 静态归零（fail-closed） |
| E3 | `deliverables/software-company/aprime2/pyo3_successor_release_map.md` | `96831cb01c5dc522108d0c818e3bd37dd0c0591b59ff8f1e5dc74de69a416900` | step2 successor/gate/slot 冻结：34 candidate 全映射 A″-01…34（自检无未映射/无重复）；disposition 与 step0 一致不重算；release-gate 快照逐项如实 | G0 applied 或可创建实施卡 |
| E4 | `deliverables/software-company/aprime2_pyo3_daemon_transport_microtask_breakdown_draft_20260827.md` | `bfc36acf34b7a128ae4e5fddb40c49d03376ba1f2f91a30422bfc378ef3a4cb7` | A″-01…37 槽位与 G1 门禁的冻结计划（§5-9），本任务 slot 映射的权威依据 | 任务执行事实 |
| E5 | `deliverables/software-company/aprime2_pyo3_daemon_transport_convergence_task_draft_20260827.md` | `3f250c046183bfc0b05a156568ea7ca4b31d3cf68f42869b6117e4fd71d5bc59` | A″ parent/G0 的 source scope、release gates、fail-closed 语义 | 任务执行事实 |
| E6 | `deliverables/software-company/aprime2_role_contracts/independent_reviewer_g0_v1.md`（+`adjudicator_g0_v1.md`、`executor_planner_g0_v1.md`） | 文本参考（revision 绑定以 daemon `role_contract_revisions` 为准） | reviewer/adjudicator/executor 固定角色合同与 PASS 门槛 | verdict 本身 |

> 无秘密约定：本表只含文件路径与 sha256，不含任何 raw credential / token / lease 值 / provider secret；Reviewer 复核时按 daemon 内 `task_verdict_events`/`task_events`/`role_contract_revisions` 对照绑定。

## 3. A″ implementation release blockers（必须如实记录，不伪称 PASS）

下列 gate 是本 G0 冻结任务**之后**任何 A″-NN 实施卡的释放前提；G0 只做冻结，**不解除**任一 blocker：

| gate | required | step0/step2 实测 | 状态 |
|---|---|---|---|
| P0-K closed | closed | `T-1787407700109-f5562c60` status=closed | **met** |
| A′ closed（含必需后裔） | closed | `T-1787293451688-c14b1e44` status=closed | **met** |
| root/route parent | active | `T-1787203926824-9f873bfc` status=in_progress | **met** |
| old S3 independent disposition | append-only disposition done | `T-1787203937208-0a795c68` status=open | **blocked/未证** |
| matrix python_compat=0 | 0 | G0 未复跑（matrix independent verification scope） | **blocked/未证** |
| live/runtime convergence | independently verified | G0 未捕获（runtime preflight scope） | **blocked/未证** |
| G0 applied | reviewer pass + adjudicator apply | pending（本 review + adjudication） | **blocked/未证** |

结论（供 Reviewer 核对）：**G0 applied 前任何 A″-NN 实施卡不可创建**；`retire_after_zero_callers` 槽位（A″-01/13/14/17/18）另需各自卡 closed 的零生产 Python caller + 无外部 ABI consumer 证明。step1 显示 `protocol_build_frame`/`protocol_parse_header` 仍被 `server/daemon_protocol.py` 生产 attr 引用、`protocol_make_ok/error_response` 仍被 `tests/test_phase4_1_daemon_protocol_diff.py` from-import → 这些 retire 槽 gate 保持 blocked。

## 4. Reviewer 核对清单（对应独立合同 PASS 门槛）

1. **162 全量**：manifest 对 162 个 PyO3 export 一项不漏、每项 disposition 唯一且可复核（E0/E1）。
2. **replace/retire successor 与 caller/ABI audit**：全部 `replace_with_http_client` 有 source-backed HTTP successor；全部 `retire_after_zero_callers` 有 exhaustive caller/ABI 审计且未越权退役（E2/E3）。
3. **边界正确**：`retain_local_core`(133，含 128 local-core)、`requires_artifact_contract`(1，A″-09 build_publish_params_py → G1 后 A″-35)、`requires_separate_authority_contract`(7) 分类边界正确；无 `unknown_blocked` 残留。
4. **零生产写入**：G0 四步未作 production source/runtime/task-state/deployment 写入（见 §1）；evidence 全为只读产物。
5. **release blockers 如实**：§3 的 A′/matrix/runtime/S3/G0-applied 状态如实记录为 blocked/未证，未伪称 PASS。

## 5. 独立审查边界（from 固定 reviewer/adjudicator 合同）

Reviewer 仅允许只读 daemon 查询与本 evidence list 对应文件、`rust_ext/src/lib.rs`、`rust_ext/src/daemon/client.rs`、`rust_ext/src/daemon_query.rs`、`server/daemon_client.py`、`server/ipc_transport.py`、`cli/`；禁止改 source/evidence/task contract、禁止建立子任务、禁止 bootstrap/apply/close、禁止部署/refresh、禁止访问 SQLite/CAS/raw credential。verdict 必须经 daemon HTTP append-only 路由，且须以独立 reviewer lease + `role_worker_v1` auth 提交。若本 G0 的 executor worker 与 reviewer worker 复用同一 worker 身份，评审应 fail-closed。

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_independent_review
  next_role: reviewer
  next_action: Conduct the bounded A″-G0 static-freeze review using evidence E0-E6; verify §3 release blockers remain accurately recorded; document verdict without deployment or implementation microtask creation.
  reason: Four executor steps produced read-only inventory/audit/successor-map/handoff evidence; no production source/runtime/task-state/deployment write occurred; implementation cards remain blocked until G0 applied and per-slot gates close.
  independence_requirement: required
```

## 6. 一致性自查

- E0 manifest 162（34 candidate + 128 local-core）与 E1/E2/E3 中的数量、disposition 计数、slot 覆盖逐项一致；本交接未重算、未改写任何 G0 冻结值。
- 本 step 未创建任何 implementation microtask（target 仅 `g0_independent_reviewer_handoff.md`），未执行部署/apply/close。
