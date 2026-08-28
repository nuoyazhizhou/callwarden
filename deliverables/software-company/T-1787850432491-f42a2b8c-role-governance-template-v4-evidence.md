# T-1787850432491-f42a2b8c：四角色入口重构证据

## 范围

本次按用户明确授权，在同一任务的文档/skill/template scope 内完成治理入口重构：

- Planner 从 Executor 混合模板中拆为独立 `Planner v1`；
- Executor 升级为 `Executor v4`；Reviewer/Adjudicator 升级为 v4；
- `cw-task-loop` 保持只读派工，不与 `g0-experiment` 合并；
- 新增共享 `role-protocol.md`，统一四角色状态、finding、decision request、远端控制台和 Handoff 语义；
- 旧 v1/v2/v3 模板移动到 `archive/role-loop/templates/legacy/`，保留历史追溯；
- 新增 `scripts/validate_template_compliance.py`，检查 AGENTS 和四份模板的最小字段与角色动作。

参考依据：`A_prime_role_comparison.md` 的 A/B/C/D/E/F 建议，以及当前 `AGENTS.md`、四个 repo skill 和 daemon 投影约束。

## 验证

```text
Skill quick_validate: cw-planner-architect -> Skill is valid!
Skill quick_validate: cw-executor-senior-engineer -> Skill is valid!
Template compliance: 模板合规检查通过: 4 个角色模板
git diff --check: pass
python -m py_compile scripts/validate_template_compliance.py: pass
```

## 部署/刷新限制

按项目规则尝试 `tokenslim run python C:/git_work/callwarden/cw.py --refresh-all`，daemon 返回：

```text
method_not_found: 未知方法: build_full_graph
```

这是当前已部署 daemon capability 缺失，未使用 SQLite/SQL fallback，也未声称数据库刷新成功。文档和模板静态验证不依赖该失败的 codegraph refresh。

## 归档清单

`archive/role-loop/templates/legacy/` 中保留 Adjudicator v1/v2/v3、Reviewer v1/v2/v3、Executor _ Planner v1/v2/v3。
归档为可恢复文件移动，不删除任务历史、verdict 或 evidence。
