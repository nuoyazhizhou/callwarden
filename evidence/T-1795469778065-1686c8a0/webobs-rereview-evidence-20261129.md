# T-1795469778065-1686c8a0 整改复评证据（webobs-rereview）

- 任务：T-1795469778065-1686c8a0（Web 观测面 MVP：本机只读项目进展看板）
- 复评对象：整改 step S-5965534c3f1d8d37fa31ed59（fix_defect，source verdict V-a61eddec9a559e14ef08da96）
- 复评身份：agent_id=reviewer-webobs-r2 / agent_instance_id=inst-webobs-rereview-20261129-01 /
  session_id=sess-webobs-rereview-20261129-01 / model_id=Sage-S2.1-Flash / role=reviewer
- 日期：2026-11-29 · 工作区：C:\git_work\callwarden（workspace_id=1 / instance=4baea3ff12c2ea5c）
- 首轮 verdict：V-a61eddec9a559e14ef08da96（BLOCKED，6 findings）

## 结论摘要

**整体 PASS**。F-1/F-2/F-4/F-5 逐条核验通过（行为级独立复测，不采信 executor 自报）；
F-3/F-6 维持披露偏差、转 adjudicator 裁决；changes-json 缺失按 F4-裁A 先例判定可追溯性
可接受；8590c73 归属异常核对为内容完整落库、记录在案，不回滚历史。

## 核项 a：8590c73 归属异常核对 —— 属实，内容完整

`git show 8590c73 --stat`：该提交（message 仅描述 T3-R-S1 evict_idle）实际含 5 文件：
本整改 4 文件（server/web_gateway.py +79、server/web_static/index.html +40、
docs/design/web-observer-read-model.md +17、evidence/.../remediation-evidence.md +88）
+ rust_ext/src/snapshot.rs（T3 自身改动）。

逐字节核对（本日实测）：
- `git diff 8590c73 -- server/web_gateway.py` → 0 行差异
- `git diff 8590c73 -- server/web_static/index.html` → 0 行差异
- `git diff 8590c73 -- docs/design/web-observer-read-model.md` → 0 行差异
- `git diff 8590c73 -- evidence/.../remediation-evidence.md` → 仅 26 行（=37d00c7 补记的
  18 行归属说明及上下文），补记前内容与 8590c73 一致

四文件 diff 内容逐 hunk 与 remediation-evidence.md 描述一致（F-1 三查询合并、F-2 固定 id
横幅、F-4 fmtAny、F-5 孤儿卡、F-1 前端置顶/truncated、read-model §3/§4/§6 更正）。
sha256 核验：remediation-evidence.md = 621e1d8b07d023b6e16928860b27fabc2c2f8312bd5c2
a9e920d05b45930ddae，与 reported 事件（event 18490）绑定的 evidence_hash 一致。
**结论：归属异常属实（message 与内容不符），但整改内容完整落库、无丢失无篡改；不回滚
历史，以本文件 + 提交内容（而非 8590c73 message）核对本整改。**

## 核项 b：F-1（blocker）修复 —— PASS（独立行为级复测）

代码核验（server/web_gateway.py:74-78, 220-288）：新增 `_ACTIVE_LIFECYCLE_STATUSES =
("open", "in_progress", "review")`；默认（无 status）看板模式合并三查询、task_id 去重、
按 created_at 倒序重排；某 lifecycle 截断时 payload 携带 `truncated` 说明；显式 status
保持单查语义。

独立复测（py -3.14，本日，网关 127.0.0.1:8793，daemon 为共享实例未重启）：
1. `GET /api/tasks`（默认）→ 200：`lifecycle_merge=["open","in_progress","review"]`、
   total=37、returned=37、**blocked_in_page=5/5**（与 executor 自报一致），5 张卡
   lifecycle 跨 open/in_progress/review 全态、均带 blocking_reasons（各 1 条）。
2. 覆盖完整性全量扫描（daemon task.list 七态逐一列举，537 行）：全库 blocked 卡恰 5 张，
   **全部落在活跃三态，终态 0 张**——三态并集即 blocked 卡完全覆盖，与 GOV-FIX-04 短路
   语义相符。首轮 6 张 vs 现在 5 张为时点差异（有卡自然流转出 blocked）。
3. `GET /api/tasks?status=closed&limit=5` → 200：total=1699、returned=5、无
   lifecycle_merge 键——单查语义保持。
4. 服务端返回序 created_at 倒序核验通过；无 truncated（37 < 100）。
5. 前端 renderTasks：blocked 置顶（稳定排序）、truncated 琥珀提示条、页脚改「活跃任务
   total / 终态卡不在看板默认页」。
6. 回归：`py -3.14 -m pytest tests/test_host_process_registry.py -x -q` → 24 passed。
7. 验证后网关进程已关闭。

## 核项 c：F-2 修复 —— PASS

server/web_static/index.html:94 固定节点 `<div id="dbStaleBanner" class="banner">`；
renderHealth（:192-202）改为 className 切换（amber 显示 / banner 隐藏），删除
`banner.after(extra)` 追加逻辑。`GET /` 返回页面含 dbStaleBanner。DOM-stub 独立复测：
db_stale 三连刷新不堆叠（after 哨兵未触发）、恢复后隐藏。

## 核项 d：F-4 修复 —— PASS

fmtAny（index.html:117-127）数组分支 `map(esc)` → `map(String)`，注释声明转义统一由
调用方出口承担。逐一核验全部调用点：kvRows:147 `esc(fmtAny(v))`、renderDashboard:221
`esc(fmtAny(v))`——无绕过转义的直出字段。全文件扫描所有模板插值：动态值（task_id/title/
reasons/status/wf/next_role/next_action/时间戳/error code/message/truncated/total/limit/
offset/step 字段/freshness/children）全部经 esc()。DOM-stub 复测：含 `a<b`、`c&d` 的数组
经 kvRows 输出 `&lt;b`、`c&amp;d`，无 `&amp;lt;`/`&amp;amp;` 双重转义。

## 核项 e：F-5 修复 —— PASS

taskRowsHTML（index.html:241-274）顶层过滤改为「parent_id 为空 **或** parent 不在页内
（!parentIds.has(p)）」均作顶层渲染；递归以 parentIds 集合传递。DOM-stub 复测：父卡不在
页内的孤儿子卡正常渲染；页内父卡恰渲染一次；正常父子树各渲染恰一次。

## 核项 f：changes-json 缺失评估 —— 可接受（按 F4-裁A 先例）

事实：整改 step S-5965534c3f1d8d37fa31ed59 的 target_file 为空（建单时未申报），task.report
的 changes 登记按 allowed_edit_scope 全等比对 fail-closed（E_CHANGE_PATH_NOT_ALLOWED），
change_audit 无本整改记录（实测仅首轮 3 条 CA-*）。变更未进 changes-json。

先例：F4-裁A（evidence/T-1795744095778-a85ae668/reviewer-verdict-f4.json）——机制根因为
建单申报纪律而非 daemon 缺陷时，接受替代追溯链（git commit + evidence 绑定），不要求
机制修复或回填登记。

本整改的替代追溯链完整：
1. git commit 8590c73 携带全部 4 文件完整 diff（逐字节核验一致，见核项 a）；
2. evidence/T-1795469778065-1686c8a0/remediation-evidence.md（sha256 621e1d8b…DDAE）
   经 reported 事件 18490 绑定 evidence_hash，逐 finding 记录修法与验证；
3. task_steps 记录 S-5965534c3f1d8d37fa31ed59 内嵌 source_findings 全文。

**结论：可追溯性可接受，不要求补登记。** 预防性过程修复同 F4 先例：建单时 target_file
以逗号分隔申报全部涉及文件（daemon 已支持多文件声明）。

## 核项 g：executor 自报不确定点逐条结论

1. 「governance_blocked 共 5 张（reviewer 复审时点为 6 张，时点差异）」——**成立**。
   本日全量扫描确认 5 张、全落活跃三态；首轮 6 张中 1 张已自然流转。不影响修复有效性
   （修复按状态集合而非卡数保证覆盖）。
2. 「整改 4 文件被 8590c73 带走、diff 为空逐字节一致」——**成立**（核项 a 已核）。
3. F-3（package-data）不修交裁决——**状态属实**：pyproject.toml [tool.setuptools.
   package-data] 仍仅 i18n/*.json；read-model §7 验收对照表（:126）仍声称「静态目录入
   pyproject package-data」，文档内部矛盾未消。交 adjudicator：补卡或扩白名单。
4. F-6（--token 仅参数位）记录在案不修——**状态属实**：web_gateway.py:401-424 token
   仅非 loopback 启动时的存在性检查，无实际校验；设计文档 §6 明确认证排除在本卡外。
   维持知情偏差定性，交 adjudicator 记录跟踪。
5. 首轮 executor 证据（docs/reports/web-observer-mvp-executor-evidence-20261124.md，
   sha256 5a05c02e…bbfe4）「已知限制」节披露的 package-data 偏差与本次复评事实一致。

## 复测环境与命令

- 解释器：py -3.14（C:\Python314，callwarden editable 安装）
- 网关：`py -3.14 -m callwarden.server.web_gateway --port 8793`（日志 Temp/webgw_8793.log）
- DOM-stub：`node Temp/webobs_dom_stub_r2.cjs server/web_static/index.html` → 12/12 PASS
- daemon：共享实例，未重启；db_stale=True（与 executor 证据时点一致）

## Findings 处置汇总

| id | 首轮 severity | 处置 | 复评结论 |
|---|---|---|---|
| F-1 | blocker | 三查询合并 + blocked 置顶 + truncated 披露 | pass（独立复测 5/5） |
| F-2 | suggestion | 固定 id #dbStaleBanner 复用 | pass |
| F-4 | nit | fmtAny 数组去预转义，出口统一 esc | pass |
| F-5 | nit | 孤儿卡顶层兜底渲染 | pass |
| F-3 | suggestion | 未修（白名单外），披露维持 | 转 adjudicator |
| F-6 | nit | 未修（设计如此），披露维持 | 转 adjudicator |

记录级事项（不阻塞）：①8590c73 归属异常（内容完整，37d00c7 已补记）；②整改 step
changes-json 缺失（F4-裁A 替代追溯链可接受）。
