# .design 停车场（跨 feature，只追加）

> 记录「本轮不做、但确认是真问题」的条目。每条必须有：来源、事实、归属、关闭条件。
> 无归属 / 无关闭条件的条目视为**待办垃圾**，不允许进本表。

| # | 来源 | 事实 | 为什么不本轮做 | 归属 | 关闭条件 |
|---|---|---|---|---|---|
| PL-01 | skillpkg-guard R2 · F-A3 | `skills/{requirements-cw,code-review-cw,test-cw}/install.ps1:36-41` 有 `$localGates` 包内副本回退分支；技能包结构自检器 AC-04 生效后，该分支在合规仓库**恒不可达**（包内不可能存在 `references/cw-gates.md`） | 删降级路径 = 三包分发容错下降，属行为变更，超出「新增检查器」的 REQ 边界 | 下一批：技能包分发健壮性 | 三包 `install.ps1` 同步删除 `$localGates` 分支，并做一次分发影响评估（`_shared` 缺失时的报错文案要能自解释） |
| PL-02 | skillpkg-guard R5 | `skills/test-cw/references/smoke.txt` 第 5 项注释写「import 边界：模块越界导入（db/ 退休后 production 零导入的守卫）」，但 `tests/test_external_import_scope.py` 实际测的是**代码图外部符号导入范围**（`db._import_external_packages_for_lang` 等）；真正的 purity 守卫是 `tests/convergence/test_db_retire_prod_purity.py` | `smoke.txt` 为 FROZEN 只读，改动须单独变更单 + 用户确认 | test-cw 冒烟集维护 | 开变更单：改正第 5 项注释描述；同时评估是否应把 `test_db_retire_prod_purity.py` 一并纳入冒烟集（当前缺 purity 守卫） |
| PL-03 | skillpkg-guard R4 · G6.4 | `task_create` 无结构化 blocking 字段，`blocked-by` 只是 description 里的文本约定，daemon 不强制——抢跑不会被拦 | 属 daemon 治理面能力缺失，需 schema 变更与迁移，不是技能层能解的 | daemon 治理（task 依赖结构化） | daemon 提供 `depends_on: [task_id]` 字段并在 `task_next` 处拦截未满足依赖的认领 |
| PL-04 | skillpkg-guard R0 停车场 | `check_skill_packages.py` 尚未接进 `cicd/check_ci_gates.py` 汇总，也未挂 CI job | 本 REQ 的 In 范围只到「有命令 + 有测试」；接 CI 涉及 workflow 改动与失败归因设计 | CI 门禁汇总 | 独立 job 或并入 `check_ci_gates.py`，并在 PR 模板/AGENTS.md 写明运行时机 |
| PL-05 | skillpkg-guard R3 · design-gate #5 | 自检器只查 `install.ps1` 里有无 `_shared` + `cw-gates.md` 两个标记，不查注入**目标目录**是否正确 | 语义校验需跑 PowerShell 或解析 AST，成本远超收益；本检查定位是「防忘记」不是「防写错」 | 技能包分发健壮性（与 PL-01 同批） | 出现一次真实「注入到错目录」事故后再评估；届时优先做「安装后自检」而非静态解析 |
| PL-06 | 2026-11-22 全局重检 | registry 共 202 行 / 192 个归一化 root。`c:/git_work/callwarden` 一个目录占 **10 行**（2 行 `c:/…` + 8 行 `\\?\C:\…` 扩展路径形式），9 archived + 1 active；`c:/git_work/tokenslim` 同理 2 行。**每个 root 仅 1 条 active，不存在重复 active 注册**，故不是正确性问题，只是 workspace 身份未归一化造成的卫生问题 | 归一化改的是 workspace 身份生成逻辑，牵动 register / snapshot / principal grant 三处键，属行为变更；且需同时清理历史归档行 | workspace 身份归一化 | 注册入口对 `\\?\` 前缀、大小写、分隔符做统一规范化，并有单测锁定「同目录不同书写形式 → 同一 identity」；历史归档行清理另开迁移 |
| PL-07 | 2026-11-22 全局重检 | `cw daemon status 1193` / `status 908` 均返回 `E_PRINCIPAL_WORKSPACE_FORBIDDEN`（principal `pr_3a44c53b…`）；同一命令传 hex 实例 `4baea3ff12c2ea5c` 正常返回三元绑定，`publish 4baea3ff12c2ea5c` 亦正常。事实面：`workspace_principal_grants` 的键域是 task_db id（`1`）、外部 id（`175`/`ws_ext936`）与路径串（`c:\git_work\callwarden`），**不含 registry 数字 id** | 属 principal 授权面语义（安全取证 #1–#5 刚落地），改键域须与 ADR-002 §5.4 对齐，不能顺带改 | daemon principal 授权面 | `status`/`publish` 的参数域与 grant 键域对齐（或 `status` 内部按 instance 解析后鉴权），并补「数字 registry id 调用」的正向 + 负向测试 |
| PL-08 | ws-identity R0 | test-cw 验收执行无 worktree 隔离：验收在开发工作区跑会吸入在途未提交改动，证据不纯；AGENTS.md 仅对 capture 有 worktree 纪律（L216-217），验收执行没有强制 | 属 test-cw 执行语义，与 workspace 身份归一正交；本轮聚焦身份层，验收隔离需 test-cw 流程改动 | test-cw 验收隔离 | test-cw 强制在干净 worktree / 独立分支执行验收（或等价 whitelist 机制），并有验收证据佐证 |

---

## 关闭记录（只追加，2026-11-22）

| # | 关闭事实 | 出处 |
|---|---|---|
| PL-01 | **已关闭**。三包 `install.ps1` 删除 `$localGates` 回退分支，改为 `_shared` 缺失即 `throw`（文案明说"包内副本不再被接受"）。分发影响评估：唯一变化是「单独分发某包且手造 `references/cw-gates.md`」这条路径从"警告+可能过期"变成"硬失败"——该路径本就违反 AC-04，硬失败是正确方向。附带修正：写回时补回 UTF-8 **BOM**（PS 5.1 读无 BOM 的 UTF-8 会把中文注释按 ANSI 解，曾险些引入回归）；三包 `PSParser::Tokenize` 均 PARSE_OK。 | 提交 `refactor(skills): install.ps1 删除 $localGates 死分支` |
| PL-02 | **已关闭**。`smoke.txt` 第 5 项注释改为其真实职责（「外部符号导入范围」）；新增第 6 项 `tests/convergence/test_db_retire_prod_purity.py`；STATUS 记 r2（6 项 / 167 例），r1 痕迹保留在文件头。实跑 167 例全绿 rc=0。 | 变更单 CL-03；提交 `27f13ef` |
| PL-03 | 未关闭（daemon 治理面能力缺失，需 schema 变更）。本轮仅确认影响面：`blocked-by` 文本约定在 `cw task show` 可原样读回，但 daemon 不拦抢跑。 | `task-map.md` §依赖声明 |
| PL-04 | 未关闭（未接 CI，本 REQ In 范围外）。 | — |
| PL-05 | 未关闭（与 PL-01 同批评估后确认：静态标记检查足够，语义校验不做）。 | `design-gate.md` 第 2 轮缺点 #5 |
| PL-06 | 未关闭。本轮仅做**定性收敛**：证明「202 行」是归一化卫生问题而非重复注册（每 root 唯一 active）。不清理、不改身份逻辑。 | 复核脚本口径：strip `\\?\` + 反斜杠归正 + 小写 + 去尾斜杠后按 root 分组 |
| PL-07 | 未关闭。本轮仅**记录调用矩阵**（数字 id 拒 / hex 实例通 / publish 通），不改鉴权域。 | `cw daemon status` 与 `publish` 实测输出；`workspace_principal_grants` 只读投影 |
