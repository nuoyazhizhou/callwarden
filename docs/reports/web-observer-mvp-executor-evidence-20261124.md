# Web 观测面 MVP — Executor 证据清单

- 任务：T-1795469778065-1686c8a0（Web 观测面 MVP：本机只读项目进展看板）
- 执行身份：agent_id=executor-webobs-mvp-01 / agent_instance_id=inst-exec-webobs-01 /
  session_id=sess-webobs-20261124-1107 / model_id=glm-5.3-flash / role=implementer
- 日期：2026-11-24 · 源码提交：`1fc671af86eb675c45f2648866f70061536c6f69`（master）

## 交付物与哈希

| 文件 | SHA-256 |
|---|---|
| docs/design/web-observer-read-model.md | 32b1a108bebe9d5e2a0bfbd65fff6503bb8d30d942f2ffec6d28c29572744793 |
| server/web_gateway.py | b845b03fd1099963e56ca739f7a0c983db81b1675f638cae0241c1f5127a5f4d |
| server/web_static/index.html | 421643bbac14d8bd956e6584440ad050847e513b9693df47c571f1a363333853 |

## 步骤回执

| Step | Step ID | 回执 request_id | 结果 |
|---|---|---|---|
| 0 读模型映射 | S-1795469778070-16d932ac | req-fdb80f769851 | success |
| 1 网关骨架 | S-1795469778070-16d9e468 | req-82ae4ca887ff | success |
| 2 单页看板 | S-1795469778070-16da35a8 | req-01ce43f3524c | success |

## 行为级验证证据（2026-11-24 实测）

1. `python -m callwarden.server.web_gateway`（py3.14.3）启动成功，GET / → 200；
   非内联依赖：仅 starlette 1.3.1 / uvicorn 0.49.0；`node --check`（v22.22.2）内联 JS 语法通过。
2. `/api/dashboard` 返回 7 section 齐全：overview / code_scale / code_quality /
   call_graph / task_risk / audit / evolution（quick 模式下 evolution=None，页面渲染
   「暂无数据」槽位，不隐藏）。daemon 拥塞期实测超时 → fail-closed 503 结构化错误，
   缓解后 200（22.7s；对应任务板上 fs_handlers.rs:handle_build_graph_async 拥塞 backlog 卡）。
3. `/api/tasks?limit=200`：total=1683，其中 5 张 governance_blocked 卡全部可见且带
   blocking_reasons（"Task Contract 缺失、多版本冲突或 revision 链不连续（无法验证 hash）"）；
   行内嵌字段 workflow_status / current_role / next_role / next_action / review / governance。
4. `/api/tasks/T-1795466501274-269961ec` 下钻：tree.status=review、
   next_action.decision=BLOCKED、workflow_status=governance_blocked、证据新鲜度区正常。
5. `/api/health` workspace 横幅实测：task workspace_id=1 /
   workspace_instance_id=4baea3ff12c2ea5c / daemon registry_workspace_id=1193 /
   registry_instance_id=task_db_instance_id=4baea3ff12c2ea5c —— 双命名空间齐全。
6. fail-closed 实测：以未注册 cwd（WorkBuddy 会话目录）启动 → workspace 解析 0 候选
   → 503 结构化错误（E_HTTP_DAEMON_UNAVAILABLE），不猜测不回退；
   `--host 0.0.0.0` 无 token → 拒绝启动（exit 2）。
7. daemon / rust_ext 零改动（提交仅含本卡 3 文件，858 行；未触碰工作区其他在途改动）。

## 已知限制（交 reviewer/adjudicator 裁决）

- 任务描述技术约束提及「静态目录加入 pyproject 的 package-data」，但本卡步骤白名单
  仅 3 个文件（E_CHANGE_PATH_NOT_ALLOWED 全等比对语义，C-22 先例），未实施。
  editable 安装下运行时无影响；wheel 打包会缺 server/web_static/index.html。
- SSE 实时刷新、写面（note/reopen）、LAN 认证按任务卡明确排除，待后续建卡。
