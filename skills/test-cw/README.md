# test-cw 技能包

跨宿主分发的**验收执行**技能：把 `ac-map.md` 里的每条 AC 变成一份第三方不重跑也能判读的证据。

一句话：**它不写测试、不修代码，只回答"这条 AC 到底跑过没有、跑出来是什么"。**

## 三件核心事

1. **三源反查选测试集**：`ac-map`/`test-plan`（源 A）∪ 符号三类字面量（源 B：snake 符号名 / **kebab CLI 参数名** / i18n key）∪ 强制并入（MCP 注册、RPC schema、i18n、`cicd/*`、文档门禁）∪ 冻结冒烟集。
2. **六条件判据**：不认"rc=0 就算过"——required nodeid 必须全 passed、required 集 skip/xfail=0、`deselected ∩ required = ∅`、`collected == len(required)`、run/code revision 一致且零漂移。
3. **证据可审计**：`evidence/<AC-id>.md` 八段（含必须全列的 nodeid 完成表），骨架由 junit 生成，人工只写判读。

## 支持的宿主

| 宿主 | 安装位置 | 常驻方式 |
|---|---|---|
| OpenAI Codex 桌面版 / CLI（共享配置） | `~/.codex/skills/test-cw/` + `~/.codex/AGENTS.md` 注入 | 按 description 自动触发 + 全局指令兜底 |
| Claude Code | `~/.claude/skills/test-cw/` | 技能自动触发 |
| Trae CN | `~/.trae-cn/skills/test-cw/`（存在才装） | 技能自动触发 |

## 安装

```powershell
# 在本目录下执行
powershell -ExecutionPolicy Bypass -File install.ps1
```

幂等；重启宿主会话后生效。**不写 .gitignore**——证据要随 git 跟踪。分发时须连同 `skills/_shared/` 一起拷贝（缺了脚本 fail-closed 报错）。

## 前置条件

- **只用证据模式**：无需 callwarden，能跑 `pytest` / `cargo test` 即可。
- **建单模式**（缺口/失败建单）：需 daemon + MCP 连接器 + `cw workspace register` 绑定 + `agent.register` 注册身份；门禁见 `references/cw-gates.md`（安装时注入）。
- **输入**：`ac-map.md`（必需；无则从 `.specs/<feature>/spec.md` 重建并标注）。`test-plan.md`、`symbol-anchor.md` 缺失时本技能现场补并声明降级。

## 目录

```
SKILL.md                     五步流程 + 六条件判据 + 边界（做薄）
references/
├── evidence-spec.md         八段 schema + junit 生成方式 + PASS/FAIL/N/A 示例 + 禁止写入
└── smoke.txt                冻结冒烟集（当前 PROPOSED，需确认后改 FROZEN）
install.ps1                  三家宿主安装 + AGENTS.md 注入 + 共享门禁注入
DESIGN.md                    立项稿 v0.4（含 backlog-46 真实试跑校准的 6 处判据修订）
```

包外相关件：`../README.md`（ALM 技能地图）、`../_shared/cw-gates.md`（G0-G6 单一事实源）。

## 试用记录（决定判据形态的那次）

在 `f8b8588`（§46 报告型命令 rc 语义）上真跑：149 例全绿、6 条 AC PASS + 1 条 manual N/A → **需求未关闭**（N/A 不算绿）。工件见 `.design/test-cw-dryrun-20261122/`，六处形态差异见 `DESIGN.md §14`。

其中最关键一条：只按符号名反查测试会 **0 命中**（真实入口是 argv 里的 `--fail-on-findings`），会被误判成 `GAP` 并造出假缺陷——所以源 B 必须扫 kebab 形态。

## 升级 / 卸载

- 升级：覆盖为本目录新版本后重跑 `install.ps1`（刷新 AGENTS.md 注入段）。
- 卸载：删除各宿主 `skills/test-cw/`，并删除 `AGENTS.md` 中 `<!-- test-cw:begin -->` 到 `<!-- test-cw:end -->` 段。
