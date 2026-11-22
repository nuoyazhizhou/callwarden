# callwarden 共享门禁与建单契约（单一事实源）

> **本文件是唯一事实源**：`requirements-cw` 与 `code-review-cw` 共用，两包不得各自内嵌一份。
> 仓库内路径 `skills/_shared/cw-gates.md`；两个 `install.ps1` 各自注入为安装包的 `references/cw-gates.md`（源码单一、分发独立）。
> 修改本文件 = 修改两个技能的门禁，须同步验证两包。
> 内容来源：`code-review-cw` 阶段零 + 阶段三 P1-P4，均为真实仓库 MCP 全链路实测的 fail-closed 结论。

## G0 · 工具可用性

按工具名**后缀**匹配（宿主前缀不同）：`mcp__callwarden__*`（Trae/WorkBuddy）/ `mcp_callwarden_*`（Claude Code）/ Codex 以实际注册名为准。

查不到 ≠ 技能坏了：先确认连接器已启用并重启会话。仍不可用 → 按各自 SKILL.md 的「兜底」降级，**不得假称已建单 / 已验收**。

## G1 · canonical 工作区绑定

`get_active_workspace()` 一次性取回三个值：

| 字段 | 用途 |
|---|---|
| `task_db_workspace_id` | **建单唯一权威 id**（`task_create.workspace_id`） |
| `workspace_instance_id` | 与上配对；缺 instance 被拒（`E_TASK_WORKSPACE_INSTANCE_REQUIRED`） |
| `snapshot_id` | report 时显式传入（G4） |

- 同一返回里的 `registry_workspace_id` 与 `task_db_workspace_id` **不同源**（实锤：registry=175 / task_db=10）。**禁止**拿 `list_workspaces` 的 id 猜，否则 `E_WORKSPACE_PROJECT_MISMATCH`。
- 被操作仓库根未绑定 → `resolve_workspace_pair ... matches=0`，fail-closed。**不要重试建单**，先 `cw workspace register` / `set` 建立绑定；项目目录须与已注册根**完全一致**（含盘符大小写）。
- `task_create` 的 `workspace_id` / `workspace_instance_id` 是可选参数，**优先不传**，由服务端按当前项目权威补齐；必须显式传时，只能传本门禁取到的配对值。

## G2 · 身份注册（不会自动注入）

WorkBuddy / MCP **不会自动注入** agent 身份（已实证推翻旧说法）。

- 身份五字段：`{agent_id, agent_instance_id, session_id, model_id, role}`，须先经 daemon RPC `agent.register` 注册，否则 `E_IDENTITY_REQUIRED` → `E_IDENTITY_UNREGISTERED`。
- `agent.register` **未暴露为 MCP 工具**：`sys.path.insert(0, r"C:\git_work")` → `callwarden.server.daemon_client.HttpDaemonRpcClient().call("agent.register", {...})`；参考 `tests/convergence/test_m3_concurrent_writes.py::_register_agent`。
- **会话对齐**：`task_next_step` 的 `agent_session_id` 必须与 `identity.session_id` **完全相同**；参数缺省为空值时即便身份已注册也会 `E_IDENTITY_SESSION_MISMATCH`。`task_report_step` 无此参数，但要带同一 `identity`。
- `agent_instance_id` 留空后，后续所有调用必须保持一致（`E_IDENTITY_INSTANCE_MISMATCH`）。
- **身份不跨会话借用**：谁来执行谁用自己注册的身份；交接包传的是证据，不是身份。

## G3 · 合同声明

1. `task_get_role_prompt(task_id)` 只读 → 取 contract 投影里的 `role_contract_prompt_hash`。
2. 构造 `contract_claim = {skill_id:"none", skill_version:"<按 role_contract>", prompt_hash:<当场读到的值>}`。
3. **hash 必须当场动态读取，禁止硬编码**（任何实测值只属于当时那批任务）。

`skill_id` 必须为 `none`，否则 `E_CONTRACT_SKILL_MISMATCH`；hash 不符 → `E_CONTRACT_PROMPT_MISMATCH`。

## G4 · snapshot 刷新

代码改动后用**同一** `name` / `root_path` 调 `register_workspace`（幂等，勿换名字新建工作区）刷新注册视图 → 读回新 `snapshot_id`；必要时 `get_active_workspace` 复核。

`task_report_step` 必须显式传 `snapshot_id`，否则 `E_TASK_REPORT_SNAPSHOT_REQUIRED`。

## G5 · 变更路径白名单

`task_report_step` **不要传 `changes`**：步骤的 `allowed_edit_scope` 只含单个 `target_file`，多文件/额外路径直接 `E_CHANGE_PATH_NOT_ALLOWED`。改动说明一律写进 `result`。

推论（对拆单的硬约束）：**一单一文件**；跨文件改动拆多单。

## G6 · 建单格式契约（两技能共用，防追溯断链）

两技能建出的单必须是同一种形状，否则 `task-map.md`、审查报告、验收证据三处对不齐。以下均为实测约束。

### G6.1 三个字段

| 字段 | 契约 | 理由（实测） |
|---|---|---|
| `title` | `<相对路径>:<符号> <动作/类别> <一句话>`。**用符号，不用行号**；新增文件无符号时用 `<路径>:新增 <动作/类别> <一句话>` | 行号随代码漂移，`<路径>:<行号>` 会让 task-map 与审查报告失配 |
| `description` | **首行固定 `REQ-xx`**（无对应需求记 `REQ-GAP`），其后可附优先级/严重度：`REQ-07 \| must`、`REQ-07 \| 🔴 blocker`、`REQ-GAP \| 🟡`。第二行起写实质内容 | 首行是唯一可机器判定的追溯锚点；写在中段等于没写 |
| `steps` | **非空数组**，每项含 `action` / `target_file` / `target_symbol` / `check_items` | 空 `steps` 不报错，但建出无步骤任务，executor 卡在 `task_next_step` 且无前置报错 |

### G6.2 `action` 合法值

实测无枚举校验（`task_collab_contract.rs:1746-1765` 仅校验非空），但**惯例只用这四个**：

| 值 | 用途 |
|---|---|
| `implement` | 新写 / 改造实现（requirements-cw 主力） |
| `fix` | 审查发现的修复（code-review-cw 主力） |
| `test` | 补测试 / 测试基建 |
| `audit` | 只读核查、取证、扫描复核 |

**禁止手写 `fix_defect`**：它是 daemon 整改步骤的保留值（`task_collab_lease.rs:313`），手工写会误导整改判定。

### G6.3 `check_items` 必须是字符串

`step_field`（`task_collab.rs:524-535`）对非字符串一律 `serde_json::to_string`：传数组会**静默**落库成 JSON 文本，读回是转义串，验证项在 executor 侧不可读。

```
check_items: "1) <可观察断言>\n2) <失败分支用例>\n3) <构建/测试命令>"
```

### G6.4 依赖声明

`task_create` / `task_create_subtask` **无结构化依赖字段**（子任务的 `parent_task_id` 只表层级，不表 blocking）。三件套兜底：

1. 建单顺序按依赖拓扑排；
2. `description` 写 `blocked-by: <task_id>`；
3. 执行侧按序认领，前序未闭环不领后续。

### G6.5 追溯闭环

`REQ-xx` ↔ 卡片（`req` 字段）↔ `task_id`（`task-map.md` 三列）↔ `AC-id`（`ac-map.md`）↔ 证据（`evidence/<AC-id>.md`）。

**整改单同样受本契约约束**：审查/扫描/验收发现的单也必须首行带 REQ，无对应需求记 `REQ-GAP`——**不得无编号建单**。

## 错误码速查

| 错误码 | 补哪个前置 |
|---|---|
| `E_WORKSPACE_PROJECT_MISMATCH` / `matches=0` | G1 |
| `E_TASK_WORKSPACE_INSTANCE_REQUIRED` | G1（instance 缺失，禁止合成 `ws-{id}`） |
| `E_IDENTITY_REQUIRED` / `E_IDENTITY_UNREGISTERED` | G2（先 `agent.register`） |
| `E_IDENTITY_SESSION_MISMATCH` | G2（`agent_session_id` 与 `identity.session_id` 不一致） |
| `E_IDENTITY_INSTANCE_MISMATCH` | G2（instance 与注册值不一致） |
| `E_CONTRACT_SKILL_MISMATCH` / `E_CONTRACT_PROMPT_MISMATCH` | G3 |
| `E_TASK_REPORT_SNAPSHOT_REQUIRED` | G4 |
| `E_CHANGE_PATH_NOT_ALLOWED` | G5（去掉 `changes` 或拆单） |

## 铁律

遇错**先补前置再重试一次**。禁止绕过门禁、禁止改 daemon 数据或直连 SQL 凑绿、禁止把未推进标成已闭环。
