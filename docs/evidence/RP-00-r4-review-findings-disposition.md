# RP-00 输入：R4 多 Reviewer finding 裁决记录

> 类型：Planner review disposition，非新设计 amendment，不是 R5。
> 规范 authority：R1～R4 经 RP-00 resolution 后生成的 merged frozen spec。
> 本文件只防止评审 finding 丢失；Executor 不得直接以本文件替代 Gate manifest 或 Task Contract。

## 1. 冻结输入

| input | SHA-256 |
| --- | --- |
| R4 `docs/design/cw-role-prompt-compiler-v1-plan-amendment-r4.md` | `8F457BB9D89445AEABECFF7D4DB9E32D8EF010C7C1D387C0DE986602176EA084` |
| 多 Reviewer 附件 `pasted-text.txt` | `2A7672F38EB9495D50D76BA309BC8BEC3D73E3241825CA1C4A45716D6DDE879D` |

附件包含三份实质评审，结论均为 R4 PASS；finding 大部分一致，没有需要推翻 R4 核心架构的 blocker。

## 2. 采纳项

### 2.1 retry class 唯一四值枚举

RP-00 frozen spec 必须 supersede R3 §9 的三值表，只保留以下四值：

- `never`；
- `after_authority_refresh`；
- `after_authority_change`；
- `bounded_transient`。

每个稳定错误码必须且只能绑定一个 retry class；CLI/MCP/Skill 不得自行重分类。

### 2.2 GATE-1A root-create 兼容边界

Gate task manifest 必须逐字写明：

```text
parent_id 缺失或规范化为空时，task.create 的 request validation、事务、返回 schema、
事件和错误语义与 Gate 前 bit-for-bit 一致；所有新增 fail-closed 检查只在 parent_id 非空分支启用。
```

同时增加 root-create golden request/response/error fixtures，防止 parent-aware hardening 回归现有 root create。

### 2.3 GATE-0 机器验收与 lease/identity 矩阵

源码已明确，无需再向用户选择：

- mutation identity 必须是 active registered Adjudicator；
- lease 必须是 anchor task 的 active reviewer lease；
- lease holder 必须是 active registered Reviewer，注册 session 与 lease session 一致；
- Reviewer 与 Adjudicator 的 agent ID、agent instance ID、session ID 必须分离；
- 请求携带真实 lease token、当前 fencing counter、task-bound evidence path/hash；
- legacy task 必须未绑定，anchor task 必须有唯一 binding/capture，requested workspace 必须 exact match anchor。

Gate manifest 的机器回读命令使用 daemon 返回值填充，不硬编码 workspace ID：

```powershell
python C:/git_work/callwarden/cw.py task next-action T-1787203926824-9f873bfc `
  --workspace-instance-id <daemon_returned_instance_id> --json
python C:/git_work/callwarden/cw.py task governance-projection T-1787203926824-9f873bfc --json
```

验收断言至少包括：不再返回 `E_WORKSPACE_AUTHORITY_UNAVAILABLE`、binding/capture/instance 可回读且一致、
重复同 request ID 幂等、不同 request 对已绑定 task 返回 already-bound、无重复 event/binding/capture。

### 2.4 GATE-1A 测试命令不得使用未定义 filter

Gate manifest 必须先冻结新 Rust test module 文件和精确测试函数名，再生成命令。不得把
`parent_aware_task_create` 当作“以后 Executor 自己起名”的占位 filter。至少冻结：

- root-create byte parity；
- governed parent success；
- parent missing/unbound；
- expected workspace mismatch；
- missing Role Contract/Contract envelope；
- identity policy unresolved；
- unknown field；
- transaction rollback/no partial child/binding/Contract/event。

### 2.5 RP-05 路径与 manifest 名称

- capability 文件实存路径为 `rust_ext/src/daemon/task_loop/capability_control.rs`，R3 中
  `rust_ext/src/daemon/capability_control.rs` 必须在 RP-00 resolution 中标记 superseded；
- 唯一机器清单名称冻结为
  `deliverables/software-company/role-prompt-v1-task-manifest.json`；删除 R3-specific 命名；
- Gate/RP manifest 生成前必须对每个 exact path 和 glob root 做存在性/父目录验证，未知路径 fail closed；
- source line count 只作 display，不参与输入身份；source identity 只以 path + SHA-256 为准。

## 3. RP-07 的确定裁决

### 3.1 两个 matrix-but-unregistered 条目不得补 MCP 注册

独立源码核验：

1. `final_zero_python_authority_audit`
   - 实际 authority 是 `mcp.final_zero_python_authority_audit` daemon RPC；
   - 调用者是 repository audit/test 通过 `HttpDaemonRpcClient` 直调；
   - 不存在 `server/tools/control_plane.py`，也没有 `@mcp.tool` 注册；
   - 这是发布 Gate RPC，不是用户 MCP tool。
2. `task_cascade_close`
   - 实际 authority 是 `task.cascade_close`；
   - public adapter 是 `cw task cascade-close` CLI/coordinator；
   - `server/tools/tools_task.py` 没有对应 `@mcp.tool`；
   - 这是聚合收尾治理 RPC，不应因 verifier 告警而新增 MCP mutation surface。

裁决：两项从 MCP tool migration matrix `M` 移除，保留在 daemon capability/operation authority 与各自测试中。
禁止为了让计数变绿而新增 Python MCP wrapper。

### 3.2 三个 registered-but-unlisted 条目必须补入 M

以下是真实 `@mcp.tool`，并已有 Rust daemon RPC：

| tool | rpc_method | op class |
| --- | --- | --- |
| `task_assignment_status` | `task.assignment.status` | `READ_ONLY` |
| `task_assignment_heartbeat` | `task.assignment.heartbeat` | `PROTECTED_MUTATION` |
| `task_governance_projection` | `task.governance_projection.get` | `READ_ONLY` |

generator source、JSON matrix 与 generated Rust mirror 必须同时新增这三行。

### 3.3 权威基线和 Prompt Compiler 增量

当前 M=241、T=242。执行“移除 2 个非 MCP RPC + 增加 3 个真实 MCP tools”后：

```text
N = |T| = |M| = 242
```

随后新增 `task_get_role_prompt`：

```text
N+1 = 243
```

数字只用于本轮 reconciliation evidence；最终 verifier 必须从 generator/source 计算，不保留硬编码 242/243。

### 3.4 删除对 298 个 dispatch extras 的全分类要求

R4 §7 条款 5 要求 `D - rpc_method(M)` 全部由 capability/operation registry 分类，会把整个 daemon RPC 清点
引入 Prompt Compiler RP-07，造成无关 scope 膨胀。RP-00 resolution 应将该条标记 `superseded`。

RP-07 唯一需要证明：

```text
names(T) == tool_names(M)
route_matrix.rs == generate(M)
rpc_method(M[rust_native|task_rpc]) subset_of D
python_compat rows 与两端 compat registry 一致
```

`D - rpc_method(M)` 属于 daemon capability registry 的独立治理问题，不是 Prompt Compiler 前置；只要它没有
通过 `@mcp.tool` 暴露，就不进入 MCP tool matrix。

## 4. 不采纳项

### 4.1 不把旧 R3 chat hash 加入 source inventory

`98C59182...` 是中间聊天稿 hash，不是当前磁盘 source，也没有 Prompt Compiler task/report/verdict。仓库检索只在
R4 narrative 中发现该前缀，当前 CLI 又没有受支持的全局 event-hash search。不能把“无法全局证明零命中”伪装成
daemon evidence，也不能用 SQL 旁路查询。

RP-00 source inventory 只记录实际冻结文件 hash；R4 §3.1 保留旧 hash 的演进说明已经足够。若未来发现正式 daemon
event 绑定旧 hash，再按该精确 task append correction，不预先制造虚假 source row。

### 4.2 不把全部 dispatch RPC 塞入 MCP matrix

dispatch=298 是信息，不是目标工具数。CLI、治理、内部和 alias RPC 可以存在；MCP tool matrix 只管理真实 MCP
registration。不得追求 `241=242=298`，也不得为了统计相等扩大 public MCP surface。

## 5. 下一步

1. R4 保持 `8F457BB9...` 不改写，不创建 R5；
2. 独立 Reviewer 对 R4 给正式 PASS 时，将本 finding disposition 作为 review 输入而非新规范；
3. PASS 后生成 Gate task manifest，吸收 §2.2～§2.4；
4. GATE-0/1A/1B 关闭后，RP-00 frozen spec/resolution 吸收 §2.1、§2.5、§3 与 §4；
5. 后续 Executor 只引用 frozen spec 和 task manifest hash，不读取本文件决定实现语义。
