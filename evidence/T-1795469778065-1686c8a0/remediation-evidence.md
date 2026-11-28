# T-1795469778065-1686c8a0 整改证据（reviewer BLOCKED → 修复）

- 任务：T-1795469778065-1686c8a0（Web 观测面 MVP：本机只读项目进展看板）
- 整改 step：S-5965534c3f1d8d37fa31ed59 / assignment：A-1bd0540e8e2d41a41c7c13f7
- reviewer verdict：V-a61eddec9a559e14ef08da96（event 937）
- 执行身份：agent_id=executor-webobs-mvp-01 / agent_instance_id=inst-exec-webobs-01 /
  session_id=sess-webobs-remediation-20261129-01 / model_id=glm-5.3-flash / role=implementer
- lease：L-740212f8348a5a3e（fencing_counter=3）
- 日期：2026-11-29

## 基线对账

原交付 commit 1fc671a 之后，web_gateway.py 被其他整改单改过两次（a729c03 F3 webui
注册钩子、b7 reconcile_stale 注释更正 b9f6b71），index.html 与 read-model 文档未动。
本整改基于当前 HEAD（dc528f4）工作树实施。

## Findings 逐条处置

### F-1（blocker）默认页 blocked 卡被挤出 —— 已修复

- 现象：前端裸取 `/api/tasks`（默认 limit=100），daemon 按 created_at 倒序返回，
  total=1735，6 张 governance_blocked 卡全挤页尾，约半天内滑出默认页、数日内零
  blocked 卡且无任何提示。违背页面副标题「不会从列表中消失」与验收 3。
- 修法（reviewer 方案①）：`server/web_gateway.py:_task_list_payload` 默认（无 status）
  看板模式改为合并 `open / in_progress / review` 三个活跃 lifecycle 查询（终态卡按
  GOV-FIX-04 短路不可能 blocked，三态并集即 blocked 卡完全覆盖）；合并后按
  created_at 倒序重排；lifecycle 截断时 payload 携带 `truncated` 说明（非静默）；
  显式 `status` 过滤保持 daemon 原单查语义。新增常量 `_ACTIVE_LIFECYCLE_STATUSES`。
- 前端配合（`server/web_static/index.html:renderTasks`）：blocked 置顶（稳定排序，
  组内保持 created_at 倒序）；`truncated` 显示琥珀提示条；分页脚注注明「活跃任务
  total / 终态卡不在看板默认页」。

### F-2（🟡）db_stale 横幅刷新堆叠 —— 已修复

- 修法：`index.html` 新增固定 `<div id="dbStaleBanner" class="banner">` 节点，
  `renderHealth` 改为 className 切换（amber 显示 / banner 隐藏），删除
  `banner.after(extra)` 追加逻辑。

### F-3（🟡）package-data 未实施 —— 不修，交 adjudicator 裁决（reviewer 指定）

- 现象：pyproject.toml 未把 `server/web_static` 配为 package-data，wheel 打包会缺
  `index.html`；read-model §7「静态目录入 pyproject package-data」声称与事实矛盾。
- 处置：按 reviewer 裁定**不在本整改单动 pyproject.toml**（超出原卡 3 文件白名单：
  server/web_gateway.py、server/web_static/index.html、docs/design/web-observer-read-model.md）。
  **请求 adjudicator 裁决**：①补一张 package-data 小卡（动 pyproject.toml +
  read-model §7 更正）；或②本卡白名单扩 1 次以纳入 pyproject.toml。editable 安装下
  运行时无影响（StaticFiles 按源码路径读），仅影响 wheel 分发。

### F-4（💭）fmtAny 双重转义 —— 已修复

- 修法：`index.html:fmtAny` 数组分支 `map(esc)` → `map(String)`，元素转义统一交给
  调用方（kvRows / renderDashboard 均对 fmtAny 结果 esc），消除 `&amp;lt;` 双重转义。

### F-5（💭）父卡不在页内则子卡不渲染 —— F-1 修复后数据验证消解 + 防御加固

- 数据核查（2026-11-29）：全量 task.list 1735 行，governance_blocked 共 5 张
  （reviewer 复审时点为 6 张，时点差异），全部落在活跃三态并集（36 行）内；
  活跃集内 parent_id 指向页外父卡的孤儿行 = 0。
- 防御加固：`taskRowsHTML` 顶层过滤改为「parent_id 为空 **或** parent 不在页内」
  均作顶层渲染——未来若出现父终态/子活跃形态，子树不再被静默跳过。

### F-6（💭）--token 仅参数位 —— 记录在案不修

- 设计文档 §6 明确将 LAN 认证排除在本卡之外，`--token` 仅参数位是既定设计，
  不属缺陷。维持现状。

## 行为级验证（2026-11-29 实测，网关 127.0.0.1:8791 动态端口）

1. `python -m callwarden.server.web_gateway --port 8791` 启动成功。
2. `GET /api/tasks`（默认）→ 200：`lifecycle_merge=["open","in_progress","review"]`、
   total=36、returned=36、**blocked_in_page=5**（5/5 全部可见，带 blocking_reasons），
   无 truncated。
3. `GET /api/tasks?status=closed&limit=5` → 200：单查语义保持（returned=5、
   total=1699、无 lifecycle_merge 键）。
4. `GET /api/health` → 200：ok=True、**db_stale=True**（本机正好处于滞后态，
   dbStaleBanner 分支真实生效路径）。
5. `GET /` → 200，页面含 `dbStaleBanner` 固定节点。
6. 前端 DOM-stub 行为测试（Node v22.22.2，10/10 PASS）：db_stale 三连刷新横幅不堆叠
   （after 哨兵未触发）、恢复后隐藏；kvRows 无 `&amp;lt;` 双重转义；blocked 置顶；
   truncated 提示条可见；孤儿卡渲染；父子各渲染恰一次。
7. 内联 JS `node --check` 通过；`python -m py_compile server/web_gateway.py` 通过。
8. 回归：`python -m pytest tests/test_host_process_registry.py -x -q` → 24 passed
   （含生产 web_gateway.main 的 webui 注册/注销集成路径）。
9. daemon 与 rust_ext 零改动；验证完网关进程已关闭。

## 产物核验

本整改不涉及二进制/构建产物改动（纯 Python 源码 + 静态 HTML + 设计文档）。

## 提交归属记录（并行会话共享工作树异常）

本整改的 4 个文件（server/web_gateway.py、server/web_static/index.html、
docs/design/web-observer-read-model.md、本 evidence）在 executor 完成 hunk 级
`git add -f` 精确暂存后、尚未执行 commit 时，被**并行会话的 T3 snapshot 整改提交
8590c73（fix(snapshot): T3-R-S1 evict_idle 候选判定删 managers 交集——消除 AB-BA
死锁, T-1795860336241-ffddc8e8 F1）一并带走提交**。该提交共 5 文件：上述 4 个
本整改文件 + rust_ext/src/snapshot.rs（T3 整改自身改动）。

处置（按并行会话纪律）：
- **不回滚历史、不 reset、不 --force**：8590c73 保持原样。
- 已核对 `git diff 8590c73 -- <4 文件>` 为空：工作树版本与提交版本逐字节一致，
  整改内容完整落库，无丢失、无篡改。
- 归属异常如实记录于此：**8590c73 的提交信息只描述 T3 snapshot 整改，未体现
  本任务（T-1795469778065-1686c8a0）的 web-observer 整改内容**。task report 与
  复审请以本文件 + 提交内容（而非 8590c73 的 message）核对本整改。
- 已通过 SendMessage 向主理人 team-lead 报告该归属异常。
