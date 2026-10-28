# Spec：跨项目任务单归属 fail-closed（CLI + MCP）

状态：待批准
日期：2026-10-28
范围：callwarden 仓库（不修改 TokenSlim 源码）

## 1. 用户目标（原文约束）

> 同一个 agent（如 Trae）同时打开多个项目：A = `c:\git_work\callwarden`，B = `c:/git_work/TokenSlim`。
> 在 A 项目里创建任务单必须挂在 callwarden；在 B 项目里创建必须挂 TokenSlim。
> 绝不允许 A 项目下给 B 项目建卡（或相反），因为 agent 操作时极易搞错。

用户已选择方案：**严格 fail-closed**——CLI 按调用项目根解析；MCP 锁定服务端项目上下文；显式跨项目 workspace 直接拒绝；给 TokenSlim 补权威绑定；加回归测试。

## 2. 现状与根因（已核实）

1. `server/daemon_client.py:3813` `resolve_workspace_pair_from_daemon(project_root=None)`：未传参时回退 `config.PROJECT_ROOT`（= callwarden 包目录，`config.py:36`）。
2. `cli/main.py:4430-4435` `cw task create` 调用该解析器时**不传 root**；而同一进程的 `db.client_workspace_root`（`cli/main.py:1033`）已由 `--workspace`/`--root`/cwd 自动检测（`cli/main.py:1220-1228`、`13121-13131`）解析为调用方项目根。
   → 在 `C:\git_work\TokenSlim` 下执行 `cw task create` 仍绑定 callwarden（已复现：返回 `workspace_id=1, instance=4baea3ff12c2ea5c`）。
3. `server/mcp_server.py:50-52` 启动即 `configure_workspace(PROJECT_ROOT)`；`server/daemon_client.py:2250-2256` 在未配置时也回退 `PROJECT_ROOT`。
   → MCP 侧永远自认 callwarden 项目。
4. `server/tools/tools_task.py:46-85` `task_create` 把 agent 传入的 `workspace_id/workspace_instance_id` 直接透传 `route_rpc`；daemon（`rust_ext/src/daemon/task_collab.rs`）只校验配对存在/实例一致，**不校验该配对是否属于调用方项目**。
   → agent 传错项目 id 时 daemon 会照单全收，这是"agent 操作必错"的直接通道。
5. `cli/main.py:1654-1659` `_mcp_callwarden_entry(root)` 只写 `command/args`，无 `cwd`/`env`；宿主 cwd 不可靠（注释明说可能是 runtime 日志目录），MCP 进程无任何项目锚点。
6. 数据侧：TokenSlim registry 存在 175 / 1288 两条同路径异形记录，显式 root 解析得到 `matches=0`，无法唯一绑定。

## 3. 设计（严格 fail-closed）

### 3.1 单一权威入口：`resolve_workspace_pair_from_daemon`

**文件**：`server/daemon_client.py`

- 保持现有签名不变（`project_root: Optional[str] = None`），但**移除隐式 PROJECT_ROOT 回退**：未传 root 时抛 `DaemonUnavailableError`，消息含 `"E_WORKSPACE_ROOT_REQUIRED"` 语义，明确要求调用方显式提供项目根。
- 理由：任何"猜默认项目"的回退都会在多项目下静默串项目，与用户要求直接冲突。
- 返回契约不变：仅返回 registry 与 task-DB 双侧一致且唯一的配对；0/多匹配继续 fail-closed。

### 3.2 CLI：按调用项目根解析 + 显式冲突拒绝

**文件**：`cli/main.py`（`_handle_task` create 分支，约 4420-4436）

新增模块级 helper（同文件内，靠近 `_handle_task`）：

```
def _resolve_task_create_project_root(db) -> str:
    root = getattr(db, "client_workspace_root", None)
    if root: return os.path.abspath(root)
    detected = detect_project_root(os.getcwd())
    if detected: return detected
    raise DaemonUnavailableError("E_WORKSPACE_ROOT_REQUIRED: 无法确定调用方项目根…")
```

create 分支逻辑改为：

1. `caller_root = _resolve_task_create_project_root(db)`
2. `caller_pair = resolve_workspace_pair_from_daemon(caller_root)`
3. 若 `--workspace-id` 或 `--workspace-instance-id` 显式给出：
   - 必须与 `caller_pair` **完全一致**，否则抛 `DaemonRemoteError`/`DaemonUnavailableError`，code `E_WORKSPACE_PROJECT_MISMATCH`，消息打印 caller_root、显式值、caller_pair 三方对比。
   - 未给出的那一半用 `caller_pair` 补齐。
4. 未显式给出：直接使用 `caller_pair`。
5. governed create（`parent_id`/`identity_policy`/`task_contract_envelope`/`task_id`）的既有"必须显式"约束保持不变，但同样要过第 3 步归属校验。

副作用：`cw task create` 在未注册项目下会 fail-closed 并提示先 `cw workspace register`，不再静默挂到 callwarden。

### 3.3 MCP：锁定服务端项目上下文 + 拒绝跨项目配对

**文件**：`server/_mcp_common.py`（新增 helper）、`server/mcp_server.py`、`server/tools/tools_task.py`

- 新增 `resolve_mcp_project_root() -> str`，优先级：
  1. 环境变量 `CALLWARDEN_WORKSPACE`（与 CLI 现有约定一致，`cli/main.py:1222`）
  2. `detect_project_root(os.getcwd())`
  3. `config.PROJECT_ROOT`（仅作为最后兜底，保持单项目用户零配置可用）
- `mcp_server.create_mcp_server()` 改为 `configure_workspace(resolve_mcp_project_root())`。
- `tools_task.task_create` 服务端前置校验：
  1. `ctx_pair = resolve_workspace_pair_from_daemon(resolve_mcp_project_root())`
  2. 若 agent 传入 `workspace_id > 0` 且 `!= ctx_pair.workspace_id` → 返回结构化错误 `{"ok": false, "code": "E_WORKSPACE_PROJECT_MISMATCH", ...}`
  3. 若 agent 传入非空 `workspace_instance_id` 且 `!= ctx_pair.workspace_instance_id` → 同上
  4. `workspace_id <= 0` 或 instance 为空 → **由服务端补齐** `ctx_pair`（agent 不再有权决定归属，daemon BR-01 仍收到完整配对）
  5. 解析失败（项目未注册）→ fail-closed 结构化错误 `E_WORKSPACE_ROOT_UNRESOLVED`，绝不回落 callwarden。
- 其余 task 工具（`task_next_step` 等）不经本次改动：它们按 `task_id` 走 `task_workspace_bindings`，已有 `_is_task_scoped_authority_request` 保护。

### 3.4 安装：项目级 MCP 配置钉住项目根

**文件**：`cli/main.py`（`_mcp_callwarden_entry` 及其调用点）

- `_mcp_callwarden_entry(root, pin_workspace: bool = False)`；`pin_workspace=True` 时追加
  `"env": {"CALLWARDEN_WORKSPACE": root}`（仅追加该键，不覆盖已有 env）。
- **仅项目级写入**（`.callwarden/agent-integrations/*`、codex 插件包 `.mcp.json`）传 `pin_workspace=True`。
- **全局合并写入**（`_write_global_mcp_config`，`cli/main.py:1875`）保持 `pin_workspace=False`：全局配置被多项目共享，钉死 root 会把所有项目都指到同一项目，属于反向 bug。
- 实现时先确认 `root` 在各调用点确实是"该项目根"（`_handle_install_agent` 中 `root = db.client_workspace_root`，行为一致）；若发现某调用点 root 是 callwarden 安装目录而非项目根，则该点不 pin，仅记录。

### 3.5 TokenSlim 权威绑定补齐（数据/运维步骤）

目标：`resolve_workspace_pair_from_daemon(r"C:\git_work\TokenSlim")` 返回唯一配对。

1. 通过 daemon RPC（`cw workspace register --root C:\git_work\TokenSlim`，禁止直接写 SQLite）让 registry 与 task DB 就同一 `workspace_instance_id` 收敛。
2. 若 `workspace.register` 因 175/1288 大小写/分隔符重复而无法幂等收敛，则在同一任务内修 registry 归一化（Rust handler 侧对 `client_view_root` 做与 `_norm_root` 一致的大小写+分隔符归一后再判重），并加针对性回归测试。
3. 收敛后记录证据：`workspace.list` 中 TokenSlim active 行唯一，且 `workspace.status` 的 registry/task-DB instance 一致。

**实现期修订（2026-10-28，用户批准）**：实测发现 registry 侧归一化已生效（175/1288 同归一化根），真正阻塞在 **task-DB 侧**：TokenSlim 的 task-DB workspace 10 的 capture 链全为 legacy `ws-10`，而 daemon `bind_task_to_workspace` 的 0c 门禁禁止同一 workspace 换 instance，导致 canonical instance（`d079f60cac8d6186`）永远无法进入 capture 域，resolver 只能看到 legacy 配对。经用户选择，采用「**扩展 daemon 做有界再认证**」：

- 新增 `TaskCollabStore::verify_canonical_adoption`（`task_collab.rs`），仅当下列**全部**成立才返回 true：
  1. 请求 instance 非空且非 legacy `ws-<digits>` 形态；
  2. 该 workspace 的 capture 链**全部**为 legacy `ws-<digits>`（空链不适用）；
  3. task-DB `workspaces.id` 存在且可读 `root_path`；
  4. daemon registry 存在该 instance 的 **active** 行，`owner_uid` 与请求 peer 一致；
  5. registry `host_real_root`（缺失退回 `client_view_root`）与 task-DB `root_path` 经 `normalize_identity_root` 后逐字节相等。
- 成立时 `task.create` 改用 `bind_task_to_workspace_adopting`：0c 放行；新 instance 的 capture
  `capture_revision` **从 1 起**（该 instance 自有链的首条），`supersedes_capture_id` 指向该 workspace
  现有最新 capture（legacy），只表达「本 workspace 权威已切换」。
- 为什么不是「workspace 最大 revision + 1」：`task_loop/next_action.rs` 的链连续性门禁按
  `(workspace_id, workspace_instance_id)` 校验 `COUNT == MAX(capture_revision)`；若新 instance 从 3 起，
  其链 count=1、max=3 会被判为断链，治理投影永远无法生成。新 instance 必须自建从 1 起的连续链。
- 任一条件不成立 → 走原 `bind_task_to_workspace`（原 0c 门禁不变，仍 fail-closed）。
- 该路径仅在 legacy→canonical 的一次性收敛场景生效；一旦链中出现 canonical instance，`legacy_chain_only` 即为 false，后续不再允许换 instance。
- 回归测试：`task_collab_tests_ws_authority.rs` 新增 3 例（正向再认证、跨根拒绝、跨 owner 拒绝），
  正向用例同时断言首个 canonical capture 恰为 revision=1、同 instance 第二次 create 后链仍连续（count == max）。
- 已落库数据修复：部署修复前 adoption 用「全局最大 + 1」写入了 TokenSlim workspace 10 的两条 canonical
  capture（revision 3、4），已就地重编号为 1、2（capture id / binding 不变），使 `COUNT == MAX` 成立；
  修复后从 TokenSlim 根新建任务追加 revision=3，链连续（count=3、max=3）。

### 3.6 回归测试

**文件**：扩展现有 `tests/test_task_create_workspace_binding.py`（不新建重复文件；若需新增用例文件则同名族 `tests/test_cross_project_workspace_binding.py`）

必须覆盖：
1. `resolve_workspace_pair_from_daemon(None)` 抛 `E_WORKSPACE_ROOT_REQUIRED`（不再回退 PROJECT_ROOT）。
2. CLI create 从 TokenSlim 根出发时，解析器收到的是 TokenSlim 根（mock `resolve_workspace_pair_from_daemon` 断言入参）。
3. CLI create 显式传入 callwarden 配对、调用方为 TokenSlim → 抛 `E_WORKSPACE_PROJECT_MISMATCH`，且 `task.create` RPC **未被调用**。
4. CLI create 显式配对与调用方一致 → 正常下发。
5. MCP `task_create`：上下文 = TokenSlim，agent 传 callwarden 配对 → 结构化拒绝且无 RPC。
6. MCP `task_create`：上下文 = TokenSlim，agent 传 0/空 → 服务端补齐为 TokenSlim 配对。
7. `_mcp_callwarden_entry` 项目级 pin env、全局级不 pin env。

### 3.7 明确不做

- 不修改 TokenSlim 仓库代码。
- 不做历史任务重绑/迁移（`task.supersede` 要求同 workspace，且本次修复后新建卡不再串项目）。
- 不触碰卡 `T-1789563951668-ba0c338c [live-e2e]`。
- 不改 daemon 侧 `task.create` 配对校验语义（本次只在客户端/服务端上下文层加归属约束）。
  - 实现期偏离（2026-10-28，用户批准）：为收敛 TokenSlim 的 legacy capture 链，daemon 增加了
    **有界再认证**（`verify_canonical_adoption` + `bind_task_to_workspace_adopting`），见 §3.5。
    该路径条件极窄（仅 legacy-only 链 + 同 owner + 同归一化根 + 一次性），0c 门禁在其它情况保持
    fail-closed 不变。

## 4. 验收标准

- AC-1：在 `C:\git_work\TokenSlim` 下 `cw task create` 成功后，`cw task show <id>` 的 workspace 归属为 TokenSlim（非 callwarden）。
- AC-2：在 `C:\git_work\callwarden` 下同样流程归属 callwarden。
- AC-3：任一项目下显式传另一项目配对 → 非零退出 + `E_WORKSPACE_PROJECT_MISMATCH`，无任务落库。
- AC-4：MCP `task_create` 在调用方项目 MCP 实例中无法创建他项目任务单（同上 code），且 0/空配对自动补为当前项目。
- AC-5：`python -m pytest tests/test_task_create_workspace_binding.py tests/test_cross_project_workspace_binding.py -q` 全绿。
- AC-6：TokenSlim 显式 root 解析返回唯一配对（matches=1），证据落 `.trae-cn/evidence/`。

## 5. 风险与回滚

- 风险：移除 PROJECT_ROOT 回退会影响所有未显式传 root 的内部调用点。实现前先 `grep resolve_workspace_pair_from_daemon(` 全量核对（已知：`cli/main.py:4431`、`cli/main.py:16298`、`tests`、Rust `task_enterprise.rs` 为独立实现）。`cli/main.py:16298` 已传 root，兼容。
- 回滚：单文件级 revert（daemon_client / main.py / mcp_server / tools_task / _mcp_common），数据侧无可逆变更（register 幂等）。
