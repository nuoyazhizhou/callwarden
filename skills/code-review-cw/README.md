# code-review-cw 技能包

跨宿主分发的代码审查技能：**图谱驱动选靶 → 只读审查 → 逐发现建 callwarden 任务单 → 全部建单后统一修复**，四阶段严格串行。门禁（canonical 绑定 / 身份注册 / 合同声明 / snapshot，均为真实仓库 MCP 全链路实测结论）与**建单格式契约**与 `requirements-cw` 共用 `references/cw-gates.md`，不各自内嵌。

**REQ 追溯**：建单 `description` 首行固定带 `REQ-xx`（取自被审仓 `.design/<feature>/ac-map.md`、`task-map.md`），无对应需求记 `REQ-GAP`；`title` 用符号不用行号。两端形状一致，审查整改单才能被需求验收与变更影响分析反查到。

## 支持的宿主

| 宿主 | 安装位置 | 常驻方式 |
|---|---|---|
| OpenAI Codex 桌面版 / CLI（共享配置） | `~/.codex/skills/code-review-cw/` + `~/.codex/AGENTS.md` 注入 | 技能按 description 自动触发 + 全局指令兜底，无需每次选择 |
| Claude Code | `~/.claude/skills/code-review-cw/` | 技能自动触发 |
| Trae CN | `~/.trae-cn/skills/code-review-cw/`（存在才装） | 技能自动触发 |

## 安装（同事侧）

```powershell
# 在本目录下执行
powershell -ExecutionPolicy Bypass -File install.ps1
```

脚本幂等可重复执行；重启宿主会话后生效。

## 前置条件

- **纯审查模式**：无需任何额外配置，装完即用。
- **建单 + 修复闭环模式**（完整工作流）：需要本机安装并运行 callwarden daemon，且在宿主中注册 callwarden MCP 连接器（工具按名后缀 `task_create` / `get_active_workspace` / `task_next_step` / `task_report_step` 等匹配）。被审查仓库需已 `cw workspace register` 完成绑定。
- **安装依赖**：门禁文件 `references/cw-gates.md` 由 `install.ps1` 从 `../_shared/cw-gates.md` 注入。分发本包时须连同 `skills/_shared/` 一起拷贝，否则脚本 fail-closed 报错（不会装出一个缺门禁的包）。

## 升级

用仓库里的新版本覆盖后重跑 `install.ps1` 即可（会覆盖旧版并刷新 AGENTS.md 注入段）。

## 卸载

删除各宿主 `skills/code-review-cw/` 目录，并删除 `~/.codex/AGENTS.md` 中 `<!-- code-review-cw:begin -->` 到 `<!-- code-review-cw:end -->` 之间的段落。
