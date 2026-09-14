# C-16 / C-17 承接卡建卡回执（2026-09-14）

- **动作**：按 [c16_c17_remediation_handoff_20260914.md](file:///c:/git_work/callwarden/deliverables/software-company/c16_c17_remediation_handoff_20260914.md)
  §5 的建卡方式，经 daemon 权威 `task.create` 创建 2 张承接卡。
- **执行基线**：`C:/git_work/callwarden`，master，HEAD = `78bb2af46e59da2ea33644fc28b2d4c6696b50d1`
  （与转交文档声明一致）。
- **本文档性质**：回执。转交工单本身**保持字节冻结**（SHA256 不变），本文件承载建卡结果。

## 1. 建卡回执

| 卡 | task_id | findings | status | Contract（r1） | steps |
|---|---|---|---|---|---|
| 卡 C | `T-1789365537146-bef4c2e4` | C-16 | `open` | `TC-T-1789365537146-bef4c2e4` / `sha256:e6d41da96fdd13a4…` | 4 |
| 卡 D | `T-1789365537230-c3f02eb4` | C-17 | `open` | `TC-T-1789365537230-c3f02eb4` / `sha256:36535bd04d6c3260…` | 4 |

- 两张卡共同项：`parent_id = T-1788871227327-45c94bd8`、`workspace_id = 1`、
  `workspace_instance_id = 4baea3ff12c2ea5c`、`identity_policy = legacy_identity_v1`、
  `governance_projection.ok = true`、三角色合同（executor/reviewer/adjudicator）r1 齐备。
- 权威投影核验：`lifecycle_status=open` / `workflow_status=queued` /
  `decision=READY` / `action=CLAIM` / `next_role=executor`。

### step 分解（两卡同构：`adjudicate → implement → test → release_verify`）

| 卡 | step0 | step1 | step2 | step3 |
|---|---|---|---|---|
| C-16 | 根因复核与调用侧注入门禁裁决 | `task_collab_lease.rs` / `handle_assignment_show` | `tests/test_c16_assignment_show_workspace_authority.py` | `rust_ext` 回归+CLI 往返 |
| C-17 | **强制前置** admin 路由 workspace 命名空间盘点 | `snapshot_state.rs` / admin route workspace authority | `tests/` 回归 | `rust_ext` 回归 |

## 2. 建卡通道与环境实测（与转交文档的差异，按 §11 诚实披露）

1. **daemon 端点**：转交文档 §5.3 / §7.1 记 `http://127.0.0.1:1615`。**实测 1615 已下线**
   （`E_HTTP_DAEMON_UNAVAILABLE` / WinError 10061）；`cw daemon health` 回执端点 = **`11338`**，
   `pid=34852`、`worker_status=healthy`、`schema_version=60`、
   `git_commit=29e0f99715d73ffcb1a817cfc3153041adf20f67`。
   本次以 health 回执为准，走 11338 建卡成功。
   → 脚本 `ENDPOINT` 已改为 `os.environ.get("CW_DAEMON_ENDPOINT", "http://127.0.0.1:1615")`，
   默认值保持历史值，运行期可用环境变量覆盖；**未**硬编码 11338。
2. **daemon git_commit ≠ HEAD**：运行中 daemon 为 `29e0f99`（C-13 治理提交），而仓库 HEAD 为
   `78bb2af`。即**运行中的 daemon 二进制早于 C-14/C-15 修复提交 `158432b`**。本回执只做建卡
   （元数据写入），不受影响；但**卡 C/卡 D 的 live 验证在部署前不可信**，必须按卡内
   `release_verify` 与转交文档 §8 部署门禁在承接卡内解决。
3. **幂等性**：脚本先 `task.list` 按 title 判重。本次 5 张既有卡回执 `exists`，仅 C-16/C-17
   为 `created`，未产生重复卡。
4. **转交文档字节冻结**：未改动 `c16_c17_remediation_handoff_20260914.md`，其
   SHA256 保持 `C0A79F5D…87111`（用户交接时声明值）；建卡结果写在本文件与 backlog §W14/§W18。
5. **backlog 回写**：
   - §W14 追加「追加裁决（2026-09-14，接续卡 A 转交）」小节含上表回执；
   - §W18 标题标记 `→ ✅ 承接卡已建`；C-16 条目补「机制表述更正」（调用侧门禁是本 HEAD 的通用
     `_is_task_scoped_authority_request()`，**非**卡 A 证据 §4.1 的 `task.`/`lease.` 前缀白名单）；
     C-17 条目补「计数差异 18 vs 19 待承接卡澄清」；
   - §W14 中「见 §W15/§W16」改为「见 §W18」（原引用指向无关小节，属笔误）。

## 3. 未做、不得声称已完成

- **未部署 runtime**：本轮未运行 `scripts/refresh_shared_runtime.ps1`，live daemon 仍为
  `29e0f99` 时代二进制；**未**做任何 live 端到端验证。
- **未开工实现**：两张卡均为 `open` / 0 step done，**未 claim、未改任何生产代码**
  （本轮唯一代码改动是建卡脚本 `create_c_bucket_remediation_tasks.py` 的 `build_cards()`
  扩表与端点环境变量化，属 `deliverables/software-company/**` 内）。
- **18 vs 19 未定论**：本文件只登记差异，**未**自行裁定哪个计数正确 —— 须由卡 D step0 盘点澄清。
- **未伪造**：未直写 SQLite、未伪造 lease/token/身份、未绕过任何 fail-closed 门禁。

## 4. 下一步（给承接该卡的 agent）

1. `cw task next-action T-1789365537146-bef4c2e4 --json`（卡 C）/ `…c3f02eb4…`（卡 D）取 step0；
2. 按转交文档 §6 走 `executor → reviewer → adjudicator` 闭环；
3. **卡 D 必须先完成 §4.3 的盘点 step 再写实现**；
4. 提交前缀用**各自卡** task_id（严禁复用 PYT 卡 `[T-1788871227327-45c94bd8]`
   或卡 A `[T-1789340885245-071cb9b4]`）。
