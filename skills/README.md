# 已迁出 → `C:\git_work\cw-alm`

本目录原先放着 ALM 交付层的三个客户端技能包与共享门禁，现已独立成仓：

| 内容 | 新位置 |
|---|---|
| 门禁唯一事实源（G1–G6） | `cw-alm/kernel/cw-gates.md` |
| 需求端 / 审查端 / 验收端 | `cw-alm/skills/{requirements-cw, code-review-cw, test-cw}` |
| 6 人格专家团 | `cw-alm/experts/callwarden-alm` |
| 结构自检 + 挂载器 | `cw-alm/tools/{check_packages,mount}.py` |

**为什么迁**：原先只有本仓能用——换项目要手抄、换宿主再抄一遍，抄完两边就漂。
现在形态是「仓库 = 源，宿主 = 投影」，漂移由 `python tools/mount.py --check` 检出（0/2/64 三态）。

```bash
cd C:/git_work/cw-alm
python tools/check_packages.py     # 结构自检
python tools/mount.py --check      # 查挂载点漂移
python tools/mount.py --apply      # 同步到 WorkBuddy
```

## 什么**没有**迁走

`.agents/skills/*`（`cw-task-loop` / `cw-planner-architect` / `cw-executor-senior-engineer` /
`g0-experiment`）留在本仓。`cw-task-loop` 被 `tests/test_role_prompt_skill.py` 跨产物契约锁定：
它的内容必须与 4 份 `docs/role-loop-templates/`、`AGENTS.md`、`docs/agent-usage-guide.md`、
`scripts/validate_template_compliance.py` 同源一致，搬走会让这条门禁退化成假门禁。
`tests/test_task_next_action_e2e.py` 也直接读它。

## 设计溯源

`skillpkg-guard` 的 R0–R5 工件（需求简报、ac-map、符号锚定、契约卡、设计门禁、任务映射、
变更单、证据）仍在 `.design/skillpkg-guard/`，`tests/test_skill_packages_guard.py` 已随
检查器迁至 `cw-alm/tests/test_packages_guard.py`。这些是本仓的历史记录，不随代码迁移。
