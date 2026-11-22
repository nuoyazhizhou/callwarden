# requirements-cw 技能包

跨宿主分发的**需求分析与拆解**技能：模糊需求 →（≤3 轮澄清）→ 可视化 → 符号锚定 → 函数级 contract 卡片 → 评审门禁 → 注入 callwarden 任务单 → 开发 → feature 级验收关闭。

核心不是"写文档"，而是三件事：

1. **门禁化评审**：每个评审点强制四件套（方案对比 ≥2 / 被否决理由 / 异常场景矩阵 8 类 / 未决项三态），从机制上防 happy path。
2. **双向追溯**：`REQ-xx → 卡片 → task_id → AC → 证据`。需求变了能反查影响面，任务能回溯需求来源。
3. **规范可执行化**：注释缺口、死函数、覆盖缺口、例外处理都用 cw 工具查，不靠提示词自觉。

## 支持的宿主

| 宿主 | 安装位置 | 常驻方式 |
|---|---|---|
| OpenAI Codex 桌面版 / CLI（共享配置） | `~/.codex/skills/requirements-cw/` + `~/.codex/AGENTS.md` 注入 | 技能按 description 自动触发 + 全局指令兜底 |
| Claude Code | `~/.claude/skills/requirements-cw/` | 技能自动触发 |
| Trae CN | `~/.trae-cn/skills/requirements-cw/`（存在才装） | 技能自动触发 |

## 安装

```powershell
# 在本目录下执行
powershell -ExecutionPolicy Bypass -File install.ps1
```

脚本幂等；重启宿主会话后生效。**脚本不写 .gitignore**——设计工件与证据需要 git 跟踪，作为基线证据链。

## 前置条件

- **出方案模式（R0-R3）**：无需 callwarden，落盘 `.design/<feature>/` 即可。
- **建单闭环模式（R4-R5）**：需要 callwarden daemon 运行 + MCP 连接器已注册（工具按名后缀匹配 `task_create` / `get_active_workspace` / `task_next_step` / `task_report_step`），仓库已 `cw workspace register` 绑定、agent 已 `agent.register` 注册身份。门禁与建单格式见 `references/cw-gates.md`（安装时由 `../_shared/cw-gates.md` 注入，与 code-review-cw 共用同一份）。

## 两条链

- **完整链（新项目）**：R0 → 技术栈选型门禁 → R1 → R2 → R3 → R4 → R5 → code-review-cw → G-Test 测试门禁 → feature 验收 → 关闭
- **精简链（存量仓迭代）**：跳过选型门禁，验收走 FA 轻量清单

## 目录

```
SKILL.md                     技能主体（六阶段流程 + 门禁插入点 + 工具映射）
references/
├── gate-card.md             评审门禁卡四件套 + 异常矩阵 8 类 + 多轮协议
├── contract-card.md         函数 contract 卡片模板 + 审查 checklist + GWT
└── change-ticket.md         基线冻结 / 变更单 / 证据目录与提交节奏
DESIGN.md                    设计文档（v2.5，含 5 轮评审修订日志、实测结论与 ALM 缝合记录）
```

**`references/cw-gates.md` 不在包内**：它是与 `code-review-cw` 共用的单一事实源，仓库源为 `../_shared/cw-gates.md`，由 `install.ps1` 在安装时注入。分发本包时须连同 `skills/_shared/` 一起拷贝（脚本缺该文件会 fail-closed 报错）。

包外相关件：`../README.md`（ALM 技能地图：各阶段归属、交接契约、已知缺口）。

## 升级 / 卸载

- 升级：覆盖为本目录新版本后重跑 `install.ps1`（会刷新 AGENTS.md 注入段）。
- 卸载：删除各宿主 `skills/requirements-cw/`，并删除 `AGENTS.md` 中 `<!-- requirements-cw:begin -->` 到 `<!-- requirements-cw:end -->` 段。
