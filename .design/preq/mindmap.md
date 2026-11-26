# 前置条件告知 — 领域概念图（R1）

```mermaid
mindmap
  root((前置条件告知))
    数据层（reuse，已存在）
      task_dependencies
        requires_existing
        requires_artifact
        provides_interface
        requires_interface
        is_informational
      dependency_edges
        is_hard=1 阻断性
        is_hard=0 信息性
        edge_type 依赖类型
      artifact_identities
        freshness_status
        producing/fresh/stale
      interface_provider_selections
        active selection
    判定引擎（new）
      evaluate_task_prerequisites
        分层判定规则
        PrerequisitesReport
        hard_unmet / informational
    投影层（extend）
      task.next_action 投影
        prerequisites 字段
      task.status 治理投影
        prerequisites 字段
    领取层（extend）
      task.claim
        E_PREREQUISITE_NOT_MET
        信息性 warning
    门禁（new）
      prerequisite_gate_v1
        capability_enabled
        capability_projection
        claim 接线
    派工层（第二批 should）
      task.work_next
        过滤未满足卡
        provider 卡推荐
    设计文档（should）
      v2 amendment 追加章节
```

## 概念关系

- `dependency_edges` 是 `task_dependencies` 的物化视图：声明写入时生成 provider→consumer 边，`is_hard` 由 `is_informational` 取反派生
- 判定引擎只读消费 `dependency_edges`（is_hard=1 得硬依赖清单）+ `tasks.lifecycle_status` + `artifact_identities.freshness_status` + `interface_provider_selections`
- 投影层与 claim 层共用同一判定引擎输出（同构保证，见 assumptions）
- capability 是 claim 写路径的启用开关：未启用 = 存量行为（不检查）；启用 = 硬依赖 fail-closed
