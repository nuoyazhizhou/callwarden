# PYT 回归残项（C-02 / W3 家族）承接交接文档（交接给下一个 agent 接力）

- **交接时间**：2026-09-15 21:30（+08:00）
- **交接基线**：`C:/git_work/callwarden`，分支 `master`，**HEAD = `a9b2fa9df9d4bb98e7e94b9701a17bf34441060c`**
- **部署运行时**：`C:/Users/wanpi/.callwarden/runtime/current/cw-daemon.exe`，sha256
  `48B58ED25EC98D391399E68D944BB10BD82D249D2E27D4F96D95B15321857451`，内嵌 `git_commit=00ca39b14e0c`
  （NF2 修复版），schema_version **60**
- **⚠️ 当前 daemon 状态：未运行**。`http://127.0.0.1:8535/health` 返回 **502 upstream connect failed**
  （桥接层在listen，daemon 进程已随上次会话退出）。接手第一件事见 §6.1。
- **交接方**：NF2 承接卡 `T-1789436399100-948b9498` 执行/复核/收尾会话（承接自 C-21 期新发现）
- **本文档性质**：**只读工单**。写给**下一个接手 agent**，使其无需人工补提示词即可建卡开工。
- **权威 finding 单源**：[pyt_regression_step4_handoff_backlog.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md)
  §W1–§W20。本文档与之一致；**冲突时以 backlog 为准并回报差异**。

> **读本文档前请先执行**（勿臆测 task_id / 勿臆测卡是否还开着）：
>
> ```bash
> PYTHONPATH=C:/git_work C:/Python314/python.exe C:/git_work/callwarden/cw.py task list --flat
> ```

---

## 0. 一句话交接

**PYT 回归卡 `T-1788871227327-45c94bd8` 的 16 张承接子卡（W12 / C-03 / C-04..07 / C-08..09 / W17 /
C-13 / C-14..15 / C-16 / C-17 / C-18 / C-19 / C-20 / C-21 / C-22 / NF1 / NF2）已 100% `closed`**，
`§W20 F1–F5` 无遗留；台账 `cw_task_commit_ledger.json` 已到 **#189**。

**接手 agent 的待做 = 三件（按优先级）**：

1. **C-02 / §W9**：`defect_learn` 的 op_class 误标 `READ_ONLY`（实为写面）——**本次实测仍在**（P2）。
2. **§W3 家族**：约 60 个「需 live daemon / manifest / snapshot 前置」的失败文件，需**以正确环境重扫**
   后定修法（P0，本次交接**未**给出可信通过率，原因见 §8 诚实披露）。
3. **运维决策项**：`rust_ext/target-*` 构建缓存清理（用户曾表示待定）、daemon 拉起（§6.1）。

---

## 1. 已完成，勿重做（接手前必读）

### 1.1 PYT 父卡 16 张子卡全部闭环（2026-09-15 21:00 实测于权威库）

| 卡 | 覆盖 finding | 关键提交 | 备注 |
|---|---|---|---|
| `T-1789274621921-e5464ad8` | W12 / C-01 | — | `query.metrics_summary` 空 workspace AVG 契约 |
| `T-1789290072972-5fad5b5c` | C-03 | — | rust_ext unix/Linux 编译阻断（已部署） |
| `T-1789290073049-6442e268` | C-04..07 + C-10..12 | `b2c6752` | cli/main.py i18n 遮蔽 + RPC 契约 |
| `T-1789290073113-6808b1ac` | C-08..09 | — | server/ 授权清单 + 错误 code |
| `T-1789301330757-87f33c34` | W17 | — | `cw collab` 治理写命令面迁 HTTP |
| `T-1789340885170-02a8fe9c` | C-13 | — | `semgrep_handlers` 编译接线 + RPC route |
| `T-1789340885245-071cb9b4` | C-14..15 | `158432be` | assignment create/revoke 契约 |
| `T-1789365537146-bef4c2e4` | C-16 | — | `assignment_show` workspace 权威解析 |
| `T-1789365537230-c3f02eb4` | C-17 | `186582e` | admin 路由块命名空间收口（已部署） |
| `T-1789377001689-0aee0a6c` | C-18 | `2a6918e` | 陈旧断言迁移 + **W3 harness 基建收敛** |
| `T-1789392878852-bb9bef18` | §W20 F1 | `e2a2853` | gc_audit 坏列（已部署） |
| `T-1789397153198-ee7baf18` | §W20 F2+F3 | `878e924` | 缺 NOT NULL UNIQUE 列（已部署） |
| `T-1789397153231-f07a8d84` | §W20 F4 | `b7fa16f` | edit/rule 第二路由块（已部署） |
| `T-1789397153261-f23f38b8` | §W20 F5 | `6986826` | 建卡模板 target_file 白名单（tests-only） |
| `T-1789436398881-877c169c` | NF1 | `5c9806b` | `gate.resolve_findings` 坏列（已部署，台账 #188） |
| `T-1789436399100-948b9498` | NF2 | `00ca39b` | `summary.generate` 版本化事务（已部署，台账 #189） |

> 复核命令（权威库直查，比 CLI 投影全）：`SELECT id,status FROM tasks WHERE parent_id='T-1788871227327-45c94bd8'`
> → 16 行，全部 `closed`。**CLI `task list` 是按 active workspace 过滤的投影，不等于全量。**

### 1.2 §W2（A 桶陈旧范式 6 文件）**实测已全绿，勿再修**

2026-09-15 21:20 交接方实测（junitxml 口径）：

```
tests/test_build_read_rpc_http.py tests/test_cli_080_http_rpc.py
tests/test_cli_090_http_rpc.py tests/test_cli_093_http_rpc.py
tests/test_cli03_task_read_authority.py tests/test_p0b_legacy_attestation_fail_closed.py
→ 37 passed / 9 skipped / 0 failed（19s）
```

backlog §W2 表里的 6 个文件**已由 `ff9bf3a` + 后续批修完**，无需再建卡。

### 1.3 部署闭环的权威口径（改产品代码必走）

- 修复提交**先于**部署（C-21 时序），使部署二进制内嵌修复 commit；
- `scripts/refresh_shared_runtime.ps1 -TaskId <T>` → 回执 JSON
  `C:/Users/wanpi/.callwarden/runtime/evidence/<ts>-<commit前缀>-<rand>.json`（**UTF-8 BOM，用 `utf-8-sig` 读**）；
- 放行判据：`status=passed` + `rollback=false` + `error=null` + `health.git_commit == HEAD`；
- **脚本退出码 1 是已知非阻塞坑**（末尾旧 core-backup 清理触发 safe-delete 批量确认门槛
  `13065>50`，三度复现）——以回执为准，不要回滚重来；
- 脚本以 **pipe 模式**拉起的 daemon **必随脚本会话终止**（四度复现）→ 手工 HTTP 重启（§6.1）。

---

## 2. 待承接工作总览

| # | 项 | 严重度 | 落点 | 前置 | 闭环后解锁 |
|---|---|---|---|---|---|
| ① | **C-02 / §W9** `defect_learn` op_class 误标 | P2 | `server/tools/tools_summary.py:492`（+ 可能 `tools_task.py` 同类） | 无 | 写面工具的正确 op_class / 幂等语义 |
| ② | **§W3 家族** ~60 文件需 live daemon | **P0** | `tests/**`（harness 已存在） | **先重扫取真实清单** | 回归全绿可验收 |
| ③ | 运维：`target-*` 缓存清理 | P3 | 工作树（未跟踪） | 用户决策 | 仓库整洁 |

**不建议合并**：①是 `server/**` 语义分类决策，②是测试基建/接受标准决策，两者 owner 与验证方式完全不同。

---

## 3. 各项规格

### 3.1 【①】C-02 / §W9 · `defect_learn` 的 op_class 标为 `READ_ONLY`

**现象（2026-09-15 交接方实测，仍在）**：

```python
# server/tools/tools_summary.py:481 / :492
def defect_learn(fix_commit_hash: str) -> dict:
    return _route('defect_learn', {"fix_commit_hash": fix_commit_hash}, 'READ_ONLY')
```

**根因**：该工具语义是**写面**——同文件 `:685` 注释自陈「`defect_learn` 为写面（INSERT `defect_fixes` /
`defect_patterns`）」，`:694` 亦记「在 worker mode=ro」。op_class 与实际语义相反 → 写操作被当只读路由
（无 request_id 幂等保护、可能被只读连接拒绝）。

**建议修法（裁决权在承接卡 step0）**：

- 首选：把 op_class 收紧为 **`PROTECTED_MUTATION`**（与其他写面工具一致），并核对该 op_class 在
  daemon 侧触发的门禁（workspace 解析 / 幂等 / 只读连接拒绝）是否与既有写面工具同源；
- **必做同型扫描**：`server/tools/*.py` 全量扫 `_route(..., 'READ_ONLY')`，逐个核对其实际语义是否写面，
  输出「可疑清单 + 依据」——**不要只改这一处**（C-02 是抽样发现，不是全量结论）；
- **不要**在未确认 daemon 侧语义前直接改，否则可能把可用工具改成被拒（blast radius 覆盖全部 READ_ONLY 工具）。

**边界（合同建议）**：

```text
allowed（建议）: server/tools/**, tests/**, deliverables/software-company/**
forbidden（沿用 C 桶一律）: db/**, rust_ext/src/**, scripts/refresh_shared_runtime.ps1,
                            direct SQLite writes, task.apply/close/supersede, status forgery
```

**验收**：

- [ ] 同型扫描清单完整（逐条给源码行号 + 语义判断依据，无空白）
- [ ] `defect_learn` op_class 落 `PROTECTED_MUTATION`，有**实测**证据：调用路径按写面门禁走
      （幂等 request_id、只读 mode 下被拒而非静默成功）
- [ ] 被判定无风险的工具逐个给出源码依据
- [ ] 相关 pytest 零新增失败（与 HEAD 基线同集对照，junitxml 为准）
- [ ] `git diff --check` clean；commit 前缀 = **本卡自身 task_id**

### 3.2 【②】§W3 · 「需 live daemon / manifest / snapshot 前置」失败族

**backlog 原描述**：剩余失败文件里约 60 个不含陈旧范式，失败签名是 `E_HTTP_MANIFEST_MISSING` /
`E_HTTP_MANIFEST_STALE` / `snapshot_not_ready` / `invalid_params: 缺少字段: workspace_instance_id` /
`E_HTTP_REQUEST_TIMEOUT`。典型：`test_mcp_*_http_rpc.py` 家族、`test_cli_task_lease_parity.py`(12)、
`test_cli_task_claim_recover.py`(9)、`test_convergence/test_m3_concurrent_writes.py`(8)、
`test_c6_snapshot_guard_replicator.py`(7)、`test_c5_s4_backup_restore_unify.py`(5)。

**已有基建（C-18 收敛，勿重造）**：

- `tests/_w3_harness.py` —— 模式 A 隔离 daemon（`USERPROFILE` 重定向 + `workspace.register` +
  task-DB seed + 空 codegraph `snapshot.publish`），B1/B2/B3 组实测 0 failed；
- 已知坑：`tests/_w3_harness.py::find_daemon_binary()` 取候选里 **mtime 最新**者 → 构建缓存目录会污染
  选型（C-18 已做 MSYS 收敛，改动前先读该文件与 C-18 证据）。

**强制前置 step：以正确环境重扫，产出真实失败清单**（本交接未提供可信通过率，见 §8）。

**两条可选路径（需用户/Planner 定，写进卡 step0 裁决）**：

1. **executor 路径（推荐）**：把 `w3_live` harness 推广到剩余家族 → 目标「本卡内零失败」；
2. **Planner 路径**：改 acceptance 为分层——tests-only 子集零失败 + 「需 daemon 族」独立卡/CI 承接。

**扫描纪律**（避免假失败，详见 §6.2）：

- 必须先 `export RUSTUP_HOME/CARGO_HOME` pin（否则任何**夹具内部调 cargo 的**用例恒 error）；
- 临时 HOME 目录必须**真实存在**（否则恒 `unable to open database file`）；
- 逐文件加 `--timeout`（有夹具会挂起：本次抽样 3 分钟未收敛）；
- `daemon` 必须已拉起（§6.1）或由 harness 自起。

**验收**：

- [ ] 重扫清单落盘（文件名 + 用例数 + 失败签名 + 归因分类），**junitxml 计数为准**
- [ ] 每条失败给出归因：`真缺陷` / `陈旧断言` / `环境前置缺失` / `需 harness 迁移`，**不得留空**
- [ ] 走路径 1 时：推广后家族零失败 + 与既有 `_w3_harness.py` 复用（不复制粘贴出第二套 harness）
- [ ] `git diff --check` clean；commit 前缀 = 本卡自身 task_id

### 3.3 【③】运维决策项

- `rust_ext/target-nf1/`（以及历史 `target-c19/`、`target-c20/`、`target-c21/`）为**未跟踪构建缓存**，
  **勿 `git add`**；是否清理需用户拍板（C 桶期已挂起一次）。
- 生产运行时保留栈：`runtime/versions/*.core-backup` 体积较大，脚本清理会撞 safe-delete 门槛；
  若要清，需显式提高阈值或分批，**不得用 `rm -rf`**（个人/系统目录禁令）。

---

## 4. 建卡方式（daemon 权威，幂等）

**唯一合法建卡通道 = daemon `task.create`**（`governance_projection ok=true`）。沿用既有幂等脚本范式，
**扩 `build_cards()`，不要新建脚本**：

| 脚本 | 用途 |
|---|---|
| `deliverables/software-company/create_c_bucket_remediation_tasks.py` | C 桶承接卡（先 `task.list` 按 title 判重） |
| `deliverables/software-company/create_nf_defect_cards.py` | NF1/NF2 独立缺陷卡（同范式，回执 `nf1_nf2_cards_receipt_20260915.md`） |

建卡参数（与既有卡一致）：

```text
PARENT_ID             = "T-1788871227327-45c94bd8"   # PYT 回归卡
WORKSPACE_ID          = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
identity_policy       = "legacy_identity_v1"          # 裸 RPC 必带；cw task create 则【禁传】
```

- **step0 建议设为 `action=adjudicate`**（盘点/裁决），`target_file` 指向**具体产物文件**（不是目录——
  §W20 F5 教训：目录级 target_file 下 `changes[]` 全等比对必拒 `E_CHANGE_PATH_NOT_ALLOWED`）；
- 建卡后**立即核对回执**：`task_id` / `governance_projection` / `status=open`，并回写 backlog 对应 §W
  的「待建承接卡」标记。

---

## 5. 角色闭环流程（每卡都要走完；**本次实测更新的参数面**）

```text
executor:   lease.acquire(role=executor) → 实现 → task.report(evidence) ×N
            → task.handoff executor_ready_for_review → reviewer
reviewer:   lease.acquire(role=reviewer)（身份与 executor 三重不同）
            → 只读独立复核 → verdict.submit(blind_first_pass, overall=pass)
            → task.handoff reviewer_pass → adjudicator
adjudicator: lease.acquire(role=adjudicator) → 独立复审
            → task.handoff adjudicator_accepted --next-role complete --next-action close
            → 用【reviewer lease】task apply → task close → 三条 lease 全 release
```

**硬约束（踩过的坑，直接抄结论）**：

- **收尾链参数面不要猜**：`SELECT reason FROM task_events WHERE task_id=? AND reason_code='handoff_structured'`
  一条查询即可拿到全量真值。定式：
  - `executor_ready_for_review` → `--next-role reviewer --next-action review`，
    `--independence-requirement required`；
  - `reviewer_pass` → `--next-role adjudicator --next-action adjudicate`，`required`；
  - `adjudicator_accepted` → `--next-role complete --next-action close`，**`not_applicable`**
    （传 `required` → `E_HANDOFF_ROUTE_INVALID`）。
- **`task.apply` / `task.close` 必须持 `--role reviewer` 的 lease**（event 侧 `role=reviewer`），
  且**不接受 `--json`**；`--lease-token` + `--fencing-counter` 缺一即拒。
- **handoff 的 `--evidence-path` / `--evidence-hash` 必须与该 task 最近一次 report 事件存储值逐字一致**：
  路径用**相对形式**（`docs/evidence/xxx.json`），绝对路径 → `E_HANDOFF_REPORT_PROVENANCE_MISMATCH`；
  hash 含 `sha256:` 前缀。写错会烧 request_id（换号重试）。
- **`--report-request-id` 取该卡最后一次 report 的 request_id**（step3 那条）。
- `task.report` 必带结构化身份四元组（`--agent-id/--session-id/--model-id/--agent-instance-id`），
  `--agent-instance-id` 须与注册值一致且全链同值；`--snapshot-id` 用卡绑定 instance 的权威 snapshot。
- **sibling 文件不在 step 白名单**时的处置 = **C-13 先例**：`changes[]` 只报白名单内文件，联动原因写进
  证据 JSON 的 `files[]`/`disclosures`，由 reviewer 判 `in_scope_acceptable`（NF2 卡 `snapshot_state.rs`
  就是这样过的）。
- **lease 语义**：raw token 不落库（只有 `token_hash`）→ **丢了不可恢复**，只能等 TTL 过期或换角色重领；
  `lease.release` 要全三元组（`--agent-id/--session-id/--model-id` 缺一 → `E_ASSIGNMENT_INCOMPLETE`）；
  投影 `WAITING / wait_for_current_lease` **不阻塞** `verdict.submit` 与 `handoff`（只影响路由显示）。
- 角色详情见 [role-protocol.md](file:///c:/git_work/callwarden/.agents/skills/cw-task-loop/references/role-protocol.md)。

---

## 6. 环境前置与已知坑（**照做，否则会得到假失败**）

### 6.1 daemon 拉起（当前未运行）

```bash
# 8535 返回 502 = 桥接在 listen、daemon 进程已死（上次会话的 pipe/后台进程随会话终止）
cd C:/git_work/callwarden
C:/Users/wanpi/.callwarden/runtime/current/cw-daemon.exe --http-bind=127.0.0.1:8535   # 后台运行，cwd 必须在仓库内
```

- 启动 ~40s 内 health 超时**属正常**（16 worker + schema init）；轮询到 200 为止；
- **cwd 必须在仓库内**，否则 health 回执里 `git_commit=unknown`；
- 校验：`health.git_commit` 应为**部署时的修复提交**（当前部署版 = `00ca39b14e0c`，即 NF2 修复 commit；
  其后 HEAD 因 docs/台账提交前移，属正常——二进制未变）。

### 6.2 跑测试的正确环境（本次实测新增，血泪）

```bash
export PYTHONPATH="C:/git_work"                 # conftest 导入 callwarden 包，缺则 ModuleNotFoundError
export CW_TEST_MODE=1 NO_PROXY=127.0.0.1,localhost
export USERPROFILE="C:/Users/wanpi/AppData/Local/Temp/cw_ci_home"
export HOME="$USERPROFILE"
export RUSTUP_HOME="C:/Users/wanpi/.rustup"     # ★ 必须 pin！否则 HOME 重定向后 rustup 找不到默认工具链
export CARGO_HOME="C:/Users/wanpi/.cargo"       # ★ 同上
mkdir -p "$USERPROFILE/.callwarden"             # ★ 目录必须真实存在，否则 unable to open database file
```

两个**环境诱导假失败**（本次实测踩到，勿误判为产品缺陷）：

| 假失败签名 | 真因 | 修法 |
|---|---|---|
| 夹具 setup error：`rustup could not choose a version of cargo to run … no default is configured` | `HOME` 被重定向 → rustup 去 `$HOME/.rustup` 找工具链 | pin `RUSTUP_HOME` |
| `internal_error: 无法打开权威库 …cw_ci_home\.callwarden\callwarden.db: unable to open database file` | 隔离 HOME 目录不存在 | `mkdir -p $USERPROFILE/.callwarden` |

### 6.3 本机沙箱/bash 限制（会给出更奇怪的假失败）

- 沙箱 bash **无 coreutils**：`basename`/`dirname`/`mkdir`/`tail`/`head`/`ls` 全缺 →
  `| tail -40` 这类管道会让 cargo 直接在 9s 内暴死且无输出；**用 python 代替**；
- `scripts/msvc-env.sh` 因此**不可 `source`**（依赖 `basename`）；自建 shebang shim 也无效——
  `#!/bin/bash` 会被 Windows 打到 **WSL bash** 并触发安全策略拦截（程序黑名单）。

**MSVC 环境改用 python 探测 + eval 注入**（已验证 = VS2022 Community 14.44.35207 + SDK 10.0.26100.0）：

```bash
eval "$(python C:/Users/wanpi/AppData/Local/Temp/msvc_env_emit.py)"
export PATH="$MSVC_POSIX_BIN:$PATH"
command -v link.exe    # 应指向 MSVC bin，而不是 Git 的 GNU link
```

（`msvc_env_emit.py` 为本次会话产出的临时脚本；若已丢失，按 msvc-env.sh 的探测逻辑用 python 重写即可：
glob `…/2022/*/VC/Tools/MSVC/*` 与 `…/Windows Kits/10/Lib/*` 取最新，导出 `LIB/INCLUDE` 为**含空格需加引号**
的 Windows 路径分号串。）

### 6.4 其它

- `git add` 只加白名单路径；**禁 `git add .` / `-A`**（工作树有大量未跟踪物：构建缓存、node_modules 等）；
- 台账 `cw_task_commit_ledger.json` 格式纪律：`indent=2` + **CRLF** + 尾换行 + `ensure_ascii=False` +
  **无 BOM**，且**单独 commit**（message 含 `[<task_id>]`）。`git add` 时 CRLF→LF 告警属正常
  （磁盘保持 CRLF）；
- **同一文件禁止并行 Edit**：同一消息里对同一文件发两个 Edit 会互相覆盖（本次实测丢过一处改动）——
  批量修改用**单次原子脚本**（逐锚点 `count==1` 守卫 + 写后即时复验）。

---

## 7. 一页速查（给接手 agent 的最短路径）

```bash
REPO=C:/git_work/callwarden ; PY=C:/Python314/python.exe
cd $REPO
export PYTHONPATH="C:/git_work" CW_TEST_MODE=1 NO_PROXY=127.0.0.1,localhost
export USERPROFILE="C:/Users/wanpi/AppData/Local/Temp/cw_ci_home" HOME="$USERPROFILE"
export RUSTUP_HOME="C:/Users/wanpi/.rustup" CARGO_HOME="C:/Users/wanpi/.cargo"
mkdir -p "$USERPROFILE/.callwarden"

# 0) daemon 是否活着（502 = 死了，见 §6.1）
python -c "import urllib.request,json;print(json.loads(urllib.request.urlopen('http://127.0.0.1:8535/health',timeout=8).read())['git_commit'])"

# 1) 权威状态（勿臆测）
$PY cw.py task list --flat
$PY cw.py task next-action <task_id> --workspace-instance-id 4baea3ff12c2ea5c --json

# 2) 权威库直查（比 CLI 投影全）
python -c "import sqlite3;c=sqlite3.connect('file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro',uri=True);print(c.execute(\"SELECT id,status FROM tasks WHERE parent_id='T-1788871227327-45c94bd8'\").fetchall())"

# 3) 单文件验证（改完即验；junitxml 计数为准）
.venv_test/Scripts/python.exe -m pytest tests/<file>.py -q --tb=short -p no:cacheprovider \
  --timeout=120 --junitxml=/tmp/out.xml
```

**参考 skill**（本机已装，直接读）：`callwarden-adjudicator-loop`（A′ 环参数面 + 本次新增的收尾链解码与
沙箱坑）、`callwarden-daemon-authority-test-repair`（陈旧断言 A 桶修法）、`callwarden-executor-governance-pack`
（事后治理包装）、`callwarden-write-path-unwedge`（写路径楔死解楔）。

---

## 8. 诚实披露（交接方未尽事项）

1. **§W3 未给出可信通过率**：本次交接只做了**抽样**（4 文件 / 63 用例），且**首次运行因环境缺配产生
   环境诱导假失败**（12 errors + 9 failures，全部归因于 §6.2 的两条环境前置，非产品缺陷）；补 pin 后
   重跑因夹具挂起超时未收敛。**接手 agent 必须自己重扫**，不要引用本次抽样数字作为缺陷证据。
2. **§W2 的绿是真实测过的**（37P/9S/0F，19s，junitxml 落盘），可放心引用。
3. **C-02 的「仍在」仅在 `tools_summary.py:492` 一处核实**；全量同型扫描是接手卡 step0 的活。
4. 本文件由**交接方单方面撰写**，未走 daemon 治理流程（不是卡、无 verdict）；其权威性 **低于**
   backlog 与 daemon 权威库。发现不一致时**以权威库为准**并回报差异。
