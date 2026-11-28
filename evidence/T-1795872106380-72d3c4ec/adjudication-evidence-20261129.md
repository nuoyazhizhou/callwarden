# T-1795872106380-72d3c4ec 裁决证据（webobs F-3 后续：package-data + read-model §7）

- 任务：T-1795872106380-72d3c4ec（webobs F-3 后续：`server/web_static` 加入 pyproject package-data 并更正 read-model §7）
- 步骤：S-1795872106385-73263a4c（step_index=0，action=write）
- 交付 commit：`716e0c4`（pyproject.toml / docs/design/web-observer-read-model.md / evidence/T-1795872106380-72d3c4ec/evidence.md）
- 来源 verdict：`V-018acaa8450b10d8d3bae626`（task_verdict_events id=944，overall=pass，phase=blind_first_pass）
- 裁决结论：**ACCEPT** → 已 `apply`（review→applied）→ 已 `close`（applied→closed）
- 日期：2026-11-29 · 工作区：`C:\git_work\callwarden`（workspace_id=1 / instance=`4baea3ff12c2ea5c`）

## 0. 裁决官身份（独立 instance/session，非复用）

| 字段 | 值 |
| --- | --- |
| agent_id | `adjudicator-webobs-f3-01` |
| agent_instance_id | `inst-webobs-f3-adjudication-20261129-01` |
| session_id | `sess-webobs-f3-adjudication-20261129-01` |
| model_id | `custom-local:deepseek-v4-flash` |
| role（acting） | `adjudicator` |
| lease role（apply/close 持有） | `reviewer`（compat 契约，见 `docs/design/cw-agent-orchestrator.md` §3.3 / `cw-role-handoff-task-loop.md` §3.1） |

与 executor（`executor-webobs-f3b-01` / `inst-webobs-f3b-20261129-01` / `sess-webobs-f3b-20261129-01`）、
reviewer（`reviewer-webobs-f3-01` / `inst-webobs-f3-review-20261129-01` / `sess-webobs-f3-review-20261129-01`）
三方 agent_id / instance / session 均无交集（见 §2.a、§5 的 `action_identities` 记录）。

## 1. Lease 取得过程（含一次自纠与 stale 回收）

### 1.1 第一次 acquire（role=adjudicator，自纠后释放）

按"裁决官自有 lease"直觉先执行：

```bash
cw lease acquire T-1795872106380-72d3c4ec --role adjudicator \
  --agent-id adjudicator-webobs-f3-01 --session-id sess-webobs-f3-adjudication-20261129-01 \
  --agent-instance-id inst-webobs-f3-adjudication-20261129-01 \
  --model-id custom-local:deepseek-v4-flash --ttl 3600 --json
# → ok=true, lease_id=L-dd0130bea8832c39, fencing_counter=1, acquired_at=1795884909
```

随后只读核对 daemon 源码 `rust_ext/src/daemon/task_collab_lifecycle_apply.rs:118-133`（apply）与
`:226-241`（close）：两条受保护写路径均调用
`validate_lease_for_mutation(tx, task_id, "reviewer", token, counter, identity)`，
即 **apply/close 只认 `role='reviewer'` 的 active lease**（AGENTS 设计文档同口径：
"Adjudicator apply/close 必须持 reviewer lease（非 adjudicator lease）"）。
故 `role=adjudicator` 的 lease 对 apply/close 结构性不可用，先释放避免残留双 active：

```bash
cw lease release T-1795872106380-72d3c4ec --role adjudicator \
  --token 35aaf0c3f821ab85821f3801c850a1054eeef2226e1db02990e9b14e966e836b \
  --agent-id adjudicator-webobs-f3-01 --session-id sess-webobs-f3-adjudication-20261129-01 \
  --agent-instance-id inst-webobs-f3-adjudication-20261129-01 \
  --model-id custom-local:deepseek-v4-flash --json
# → {"ok":true,"lease_id":"L-dd0130bea8832c39","status":"released","released_at":1795887539}
```

lease 事件 8340：`L-dd0130bea8832c39 / adjudicator / release / counter=1 / at=1795887539`。

### 1.2 第二次 acquire（role=reviewer，触发 stale holder 回收）

```bash
cw lease acquire T-1795872106380-72d3c4ec --role reviewer \
  --agent-id adjudicator-webobs-f3-01 --session-id sess-webobs-f3-adjudication-20261129-01 \
  --agent-instance-id inst-webobs-f3-adjudication-20261129-01 \
  --model-id custom-local:deepseek-v4-flash --ttl 3600 --json
# → ok=true, lease_id=L-b4284826aa7c509f, role=reviewer, fencing_counter=2,
#   acquired_at=1795887649, expires_at=1795891249
```

本卡历史里 reviewer 落账后**未释放** `L-73e447288561e129`（fencing=1，holder 心跳停在
`1795884574.77`）。daemon 在 `task_collab_lease.rs:1284-1330` 对"未过期但 holder 心跳超
`ORPHAN_CLAIM_STALE_SECS`(=900s)"的 orphan lease 做同事务回收。本次实测心跳年龄
≈ `1795887649 - 1795884574.77 = 3074.2s` > 900s，故**一次即成功**（无 `E_LEASE_ACTIVE_EXISTS`
重试，stale 回收重试次数 = 0）：

lease 事件 8341（旧 lease 回收，与 8342 新 acquire 同事务同刻）：

```
event_id=EVT-7f1ec5f835272a15
lease_id=L-73e447288561e129  role=reviewer  event_type=expire  fencing_counter=1  event_at=1795887649
detail="stale holder recovered: reason=holder_heartbeat_stale,
        holder_agent_id=reviewer-webobs-f3-01,
        holder_session_id=sess-webobs-f3-review-20261129-01,
        holder_model_id=custom-local:deepseek-v4-flash"
```

回收后 `task_leases` 全量（只读查 `C:\Users\wanpi\.callwarden\callwarden.db`）：

```
lease_id            | role       | agent_id                  | session_id                             | fencing | status   | acquired    | expires     | released
L-839553feaed17e9a  | executor   | executor-webobs-f3b-01    | sess-webobs-f3b-20261129-01            | 1       | released | 1795882273  | 1795887673  | 1795884274
L-73e447288561e129  | reviewer   | reviewer-webobs-f3-01     | sess-webobs-f3-review-20261129-01      | 1       | expired  | 1795884577  | 1795888177  | 1795887649(回收)
L-dd0130bea8832c39  | adjudicator| adjudicator-webobs-f3-01  | sess-webobs-f3-adjudication-20261129-01| 1       | released | 1795884909  | 1795888509  | 1795887539
L-b4284826aa7c509f  | reviewer   | adjudicator-webobs-f3-01  | sess-webobs-f3-adjudication-20261129-01| 2       | active→released | 1795887649 | 1795891249 | 1795887750(用毕释放)
```

### 1.3 投影观察（如实记录，非阻塞项）

acquire（role=reviewer）成功后 `cw task next-action` 仍为 `READY / REVIEW`
（`evaluated at 1795887675.9`）。原因：`next_action.rs:1639-1665` 规则 6 优先于规则 8-10，
且 `active_lease_role()` 按 `id ASC` 命中 role=`reviewer` 的 active lease →
`review_outcome`。这是"adjudicator 持 reviewer lease"与规则 6 的结构性投影盲区，
不阻断 apply/close（两者分别校验 verdict 与 lease/token/counter，不读 next_action）。
本次未强行改动投影，按主理人约定如实记录；apply/close 完成后终态投影已为 `COMPLETE / NONE`。

## 2. 三前提核验（全部只读；全部 PASS）

只读手段：`sqlite3 "file:C:/Users/wanpi/.callwarden/callwarden.db?mode=ro"`；全程未直写 SQLite。

### a. verdict 落账 —— PASS

```
id=944  verdict_id=V-018acaa8450b10d8d3bae626  overall=pass  phase=blind_first_pass
step_id=S-1795872106385-73263a4c
contract_id=TC-T-1795872106380-72d3c4ec  contract_revision=1
normalization_version=verdict-normalization/v1
normalization_rules_hash=sha256:b41cbdb3f2882b3efc0fbbbddfb4fd5b40e23549cdbeb49af2dec798184b0e8d
submitted_at=1795884658
reviewer_identity.identity={agent_id:reviewer-webobs-f3-01,
  agent_instance_id:inst-webobs-f3-review-20261129-01,
  session_id:sess-webobs-f3-review-20261129-01, role:reviewer}
```

规则可加载性（对应 `verdict_evidence_gate.rs:validate_normalization_binding`）：

```
SELECT verdict_rule_set_id,normalization_version,rules_hash FROM verdict_normalization_rules;
→ verdict-normalization/v1 | verdict-normalization/v1 | sha256:b41cbdb3...b0e8d   （与绑定逐字相同）
SELECT COUNT(*) FROM verdict_normalization_rule_revocations
  WHERE verdict_rule_set_id='verdict-normalization/v1';
→ 0                                                                              （未撤销）
```

reviewer 五元组非空，且 agent_instance_id / session_id 与 executor、adjudicator 均不同 → 投影不会落 UNVERIFIED。

### b. lease / fencing 溯源 —— PASS

- 旧 lease `L-73e447288561e129`（fencing=1，token_hash=08cae764…e7db）已被本次 acquire 回收为
  `expired`（事件 8341，reason=`holder_heartbeat_stale`）。
- 新 lease `L-b4284826aa7c509f` fencing_counter=**2**（该 task+role 历史 MAX+1，单调递增）。
- verdict `submitted_at=1795884658` 落在旧 reviewer lease 活跃区间
  `[acquired_at=1795884577, expires_at=1795888177]` 内（差值 +81s / −3520s），
  即 verdict 由当时真实持 lease 的 reviewer 提交，非法外提交排除。

### c. handoff 证据 —— PASS

```bash
sha256sum evidence/T-1795872106380-72d3c4ec/rereview-evidence-20261129.md
→ 02bfe6bd9ddd5329ad97de832b0e55a9efa6c1da82b1a0f8de7431b168f86577
git show HEAD:evidence/.../rereview-evidence-20261129.md | sha256sum
→ 02bfe6bd9ddd5329ad97de832b0e55a9efa6c1da82b1a0f8de7431b168f86577
```

逐字节等于 task_events `18582`（`handoff_structured`，outcome=reviewer_pass，
target_role=adjudicator）记录的 `evidence_hash=02bfe6bd...6577`；工作树与 HEAD 一致（无未提交漂移）。
执行侧证据 `evidence.md` 实测 `b0552786...9369`，与事件 `18579` 记录一致。

## 3. 实质裁决（独立复核，不采信他人结论）

### 3.1 变更面 —— 恰 3 文件，无并行线污染

```
git show --name-status --format="" 716e0c4
M  docs/design/web-observer-read-model.md
A  evidence/T-1795872106380-72d3c4ec/evidence.md
M  pyproject.toml
```

`rust_ext/src/daemon/snapshot_state.rs` 不在提交内（该文件仍在工作树 modified，属并行线在途）。
`pyproject.toml` +3 行，`read-model.md` +1/−1 行，`evidence.md` +129 行。

### 3.2 F-3 缺口是否真的补齐 —— 独立构建 wheel 实测

pyproject 实际 diff（`[tool.setuptools.package-data]`）：

```toml
callwarden = [
    "i18n/*.json",
    # Web 观测面（F-3）：静态看板单文件随 wheel 发布，路径相对包根
    # （package-dir 已把 callwarden 映射到仓库根，故写成 server/web_static/...）
    "server/web_static/*.html",
]
```

`[tool.setuptools] package-dir = { "callwarden" = "." }` → 包根 = 仓库根，
故 glob `server/web_static/*.html` 与 `server/web_static/index.html` 对应，
wheel 内落 `callwarden/server/web_static/index.html`。运行期确需该文件：
`server/web_gateway.py:56` `WEB_STATIC_DIR = Path(__file__).resolve().parent / "web_static"`
（装好的包里即 `callwarden/server/web_static`），缺失会抛 "web_static 目录缺失"（:375）。

**正向（在 `git archive 716e0c4` 的干净副本中独立重建，未污染工作树）**：

```bash
git archive 716e0c4 | tar -x -C $TMP/src && cp callwarden_core.pyd $TMP/src/
python -m pip wheel . --no-deps --no-build-isolation -w $TMP/out     # rc=0
# → callwarden-0.3.23-cp313-cp313-win_amd64.whl
```

```
wheel entries: 129
target: ['callwarden/server/web_static/index.html']
wheel sha256: 9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17
源文件 sha256: 9d8b1c0d4383c8ef8bef73f0b85455445685f0e2e223c7b94e7d53eb695d5b17   （逐字节一致）
i18n entries: callwarden/i18n/{__init__.py,en_US.json,zh_CN.json}                （原条目未受影响）
```

**负向对照（父提交 10aae41 的干净副本，同环境同命令）**：

```
pyproject 无 web_static 条目(预期)
wheel entries: 128
target present: []            （缺失目标文件）
i18n: callwarden/i18n/{__init__.py,en_US.json,zh_CN.json}   （仍在）
```

即 +1 条 wheel 条目精确来自本次新增声明，排除"偶然被其它规则带入/陈旧 build 污染"。
另注：首次负向构建未放 `callwarden_core.pyd` 时，`build_py` 阶段日志已直接显示
`copying .\server\web_static\index.html -> build\lib.win-amd64-cpython-313\callwarden\server\web_static`，
与最终 wheel 落点互相印证。

### 3.3 read-model 表述与事实一致性 —— 通过

- `docs/design/web-observer-read-model.md:126`（§7 验收对照表第 6 行）改为
  "静态资源由 `[tool.setuptools.package-data]`（`callwarden` 包键）的 `server/web_static/*.html`
  条目声明，随 wheel 发布（路径相对包根，包根 = 仓库根）"——与 pyproject 实际声明逐项对齐。
- `:29` "`server/web_static/index.html`（package-data）"在本次修复后已成事实，无须改动，保持改动面最小。
- 原表述"静态目录入 pyproject package-data"在 716e0c4 之前确实与事实矛盾（当时条目只有 `i18n/*.json`），
  属真实缺口而非文档口径调整。

### 3.4 裁决结论

**ACCEPT**。三条验收（pyproject 声明静态目录 / read-model §7 与事实一致 / 验证证据落档）均有
可独立复现的行为级证据支撑；未发现实质缺口，故继续 apply/close。
（若仅"基本没问题"则不构成放行理由——本次是完成独立复算后才 ACCEPT。）

## 4. apply / close 回执

```bash
cw task apply T-1795872106380-72d3c4ec --reviewer reviewer-webobs-f3-01 \
  --agent-id adjudicator-webobs-f3-01 --session-id sess-webobs-f3-adjudication-20261129-01 \
  --agent-instance-id inst-webobs-f3-adjudication-20261129-01 \
  --model-id custom-local:deepseek-v4-flash --role adjudicator \
  --lease-token 0b5b91d7...80b0c --fencing-counter 2
# → ✓ Task applied: T-1795872106380-72d3c4ec / Status: applied / Applied at: 1795887706.0978227
# → ✓ Structured identity recorded (agent/session/model/role)

cw task close T-1795872106380-72d3c4ec ...（同身份同凭证）
# → ✓ Task closed: T-1795872106380-72d3c4ec / Status: closed / Closed at: 1795887727.990623
```

- **apply 事件：task_events `18590`**（`review → applied`，reason_code=applied，role=adjudicator，
  authoritative_timestamp=1795887706.0978227）
- **close 事件：task_events `18591`**（`applied → closed`，reason_code=closed，role=adjudicator，
  authoritative_timestamp=1795887727.990623）
- `tasks` 终态：`status=closed`，`applied_at=1795887706.0978`，`updated_at=1795887727.9906`
- 终态投影：`cw task next-action` → `Decision: COMPLETE / Action: NONE`
- 动作身份落账（`action_identities`）：
  `state_transition / adjudicator-webobs-f3-01 / sess-webobs-f3-adjudication-20261129-01 / adjudicator`
  （apply 与 close 各一条，recorded_at=1795887706.0978 / 1795887727.9906）

## 5. lease 释放（严禁残留）

```bash
cw lease release T-1795872106380-72d3c4ec --role reviewer \
  --token 0b5b91d7...80b0c --agent-id adjudicator-webobs-f3-01 \
  --session-id sess-webobs-f3-adjudication-20261129-01 \
  --agent-instance-id inst-webobs-f3-adjudication-20261129-01 \
  --model-id custom-local:deepseek-v4-flash --json
# → {"ok":true,"lease_id":"L-b4284826aa7c509f","role":"reviewer","fencing_counter":2,
#    "released_at":1795887750.0,"status":"released"}
```

lease 事件 8343：`L-b4284826aa7c509f / reviewer / release / counter=2 / at=1795887750`。
最终 `task_leases` 四条全部 `released`/`expired`，**本卡无任何 active lease 残留**（见 §1.2 表末行）。

## 6. 附：环境异常与处置（如实记录，不隐瞒）

1. **MCP `lease_acquire` 扁平字段不可用（与主理人判断一致）**：全程使用 CLI `cw lease`，未使用 MCP 工具。
2. **共享 daemon 在核验期间发生重启**：00:06:12 起运行的共享 daemon pid=13848（started_at=1795881972）；
   01:37:40 一次 CLI 调用（`cw lease release` 首次尝试）在 `workspace.register` → 发现
   `E_HTTP_MANIFEST_STALE`（manifest PID 5852 不存活）处失败，manifest 随之被改写为 pid 5852
   （started_at=1795887460）。随后该会话内 daemon 进程不存活。经只读核查确认：manifest 指向死
   PID、进程表无 cw-daemon.exe → 判定为"daemon 已不在运行"而非"存活被强杀"。
3. **处置**：使用 CLI 官方入口 `cw daemon start`（文档语义："detached 进程, client 退出后仍存活"）
   重新拉起（pid 27600 / health ok / git_commit=76d716b / schema_version=61），并在每次需 daemon
   的写操作命令内先 `cw daemon start`（本会话内 detached 子进程仍在命令结束后被回收，故同命令内前置
   拉起是唯一稳定方式）。**未使用 kill/stop、未手工改 manifest、未直写 SQLite、未 supersede/rollback。**
4. 影响评估：本卡四条 lease 与 task_events 的连续性无断裂（事件 id 单调，18590/18591 追加在
   18584 之后；lease 事件 8340→8341→8342→8343 连续）。重启导致的唯一副作用是 §1.3 所述
   acquire 后投影仍为 REVIEW，已在 apply/close 后自然收敛为 COMPLETE。

## 7. 未改动声明

- 未修改任何源码：`pyproject.toml`、`docs/design/web-observer-read-model.md`、
  `rust_ext/src/daemon/snapshot_state.rs` 均只读。
- 未 `git add .`/`-A`；本文件单独 `git add -f` 后提交。
- 未直写 SQLite；未执行 `task.supersede` / `task.rollback`。
