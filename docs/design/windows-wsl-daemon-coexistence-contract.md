# Windows、WSL 与 Linux Daemon 共存契约

> 任务：`T-1786330576149-d2cd128c`
> 状态：设计阶段
> 适用范围：Windows 主机、WSL Agent/CLI、Linux 原生 daemon，以及同一用户下的 CLI/MCP/Agent 协同

## 1. 结论先行

Windows 与 WSL 可以共同使用 Windows `cw-daemon`，但 **WSL 不得直接打开
Windows 权威 SQLite**。Windows Named Pipe 是 Windows 进程的本地传输端点，不能
被当成 WSL 中的普通 UDS 文件；WSL 需要通过一个明确的 Windows 侧客户端/桥接层
访问它。

同时，WSL 也可以运行自己的 Linux `cw-daemon`，但它必须拥有独立的 Linux 数据根、
独立的任务库和独立的 UDS。两个 daemon 是两个 authority，不能共同写同一个数据库、
同一个 workspace 或同一份 staging/WAL 文件。

核心规则：

1. **Authority 与 Transport 分离**：先决定 workspace/任务库由哪个 daemon 权威管理，
   再决定通过 Named Pipe、UDS 或 bridge 连接。
2. **一个 authority 一个 SQLite 单写点**：所有 task、task_step、task_event、
   change_audit、refresh/index、snapshot/CAS 写入都必须到该 authority 的 daemon。
3. **不自动跨 authority 故障切换**：Windows daemon 不可用时，Windows workspace 的
   写请求必须 fail-closed，不能偷偷写 WSL 本地库。
4. **WSL 原生 workspace 与 Windows workspace 分开登记**：`/home/...` 或 WSL ext4
   上的 workspace 可以归 WSL daemon；`C:\...`/`/mnt/c/...` 上且由 Windows 侧维护的
   workspace 归 Windows daemon，不能仅凭路径字符串猜测 authority。

## 2. 当前实现与缺口

当前代码已经具备以下基础：

| 能力 | 当前实现 | 结论 |
|---|---|---|
| Windows daemon 端点 | `\\.\\pipe\\callwarden-<SID>` | 同一 Windows 用户下 CLI/MCP/Agent 可共享 |
| Linux daemon 端点 | `/run/callwarden/callwarden.sock` 或 `CW_DAEMON_SOCKET` | Linux 原生进程可通过 UDS 共享 |
| task 单写路由 | `CW_TASK_WRITE_POLICY=shared` + daemon route | 防止同平台 Python task 直写 |
| daemon 自动启动 | Windows detached process；Unix 外部服务/本地启动 | 仅解决当前进程平台的端点 |
| 跨 Windows/WSL 连接 | 无正式 bridge 契约 | **当前缺口** |
| authority 选择 | 主要按 `sys.platform` 和默认 endpoint | **不能表达 WSL 访问 Windows authority** |
| 双 daemon 共存 | 可分别启动，但无 authority/workspace 注册契约 | **需要补齐** |

特别注意：`CW_DAEMON_MODE=auto` 只表达“当前客户端如何尝试 daemon”，不表达
“使用 Windows authority 还是 WSL authority”。把它当作跨平台共享开关是不正确的。

## 3. 术语与身份

### 3.1 Authority

每个 daemon 启动时拥有一个稳定的 `authority_id`，至少包含：

```text
<host-instance>/<platform>/<user-or-service>/<database-fingerprint>
```

示例：

```text
desktop-01/windows/user-S-1-5-21-.../db-<hash>
wsl-ubuntu/linux/uid-1000/db-<hash>
```

`authority_id` 不由客户端自由声称；由 daemon 在 `ping`/`hello` 响应中返回。客户端
必须把它写入连接上下文和审计结果。

### 3.2 Transport

Transport 只描述“怎么到达 daemon”：

| Transport | 典型端点 | 适用方 |
|---|---|---|
| Windows Named Pipe | `\\.\\pipe\\callwarden-<SID>` | Windows 本地 CLI/MCP/Agent |
| Linux UDS | `/run/callwarden/callwarden.sock` | Linux/WSL 原生 daemon 客户端 |
| Windows bridge | `127.0.0.1:<port>` 或经验证的 Windows AF_UNIX | WSL 客户端访问 Windows daemon |
| CLI bridge | WSL 启动 Windows `cw.exe`/Python 客户端 | 最小可用过渡方案 |

Transport 不能改变 authority。bridge 只是 Windows Named Pipe 的受限代理，不拥有
第二份任务库，也不执行 SQLite 操作。

### 3.3 Workspace binding

workspace 注册结果必须包含：

```json
{
  "workspace_instance_id": "...",
  "authority_id": "...",
  "host_kind": "windows|wsl|linux|macos",
  "root_namespace": "windows-path|wsl-ext4|linux-native",
  "root_path": "...",
  "write_owner": "daemon-only"
}
```

同一个 `workspace_instance_id` 只能绑定一个 authority。发现绑定冲突时必须返回
`E_WORKSPACE_AUTHORITY_CONFLICT`，不能选择“最后连接的 daemon”。

## 4. 推荐部署模型

### 4.1 Windows 主机 authority

这是当前用户 Windows 开发环境的默认模型：

```text
Windows CLI/MCP/Agent
        │ Named Pipe
        ▼
Windows cw-daemon
        │ 单写
        ▼
Windows 用户级 ~/.callwarden/callwarden.db
```

Windows daemon 负责：

- Windows 用户的 task、task_step、task_event、change_audit；
- Windows workspace 的 refresh、索引、snapshot、CAS；
- Windows CLI/MCP/Agent 的共享并发写；
- 为 WSL bridge 提供同一套 RPC，不向 bridge 暴露 SQLite 文件。

### 4.2 WSL Agent 访问 Windows authority

正式方案是增加 Windows 侧 `cw-bridge`，而不是让 WSL 直接访问
`/mnt/c/Users/<user>/.callwarden/callwarden.db`：

```text
WSL Agent/CLI
    │ bridge protocol（loopback + token，或已验证 AF_UNIX）
    ▼
Windows cw-bridge
    │ Windows Named Pipe
    ▼
Windows cw-daemon
    │
    ▼
Windows authority DB
```

bridge 的职责只有：

1. 从 WSL 接收有界 JSON-RPC 请求；
2. 用安装时生成、ACL 仅当前 Windows 用户可读的 token 做本地认证；
3. 将请求原样转发到当前用户 SID 的 Named Pipe；
4. 将 daemon 的结构化响应原样返回；
5. 记录 bridge connection、authority_id、request_id 和错误码。

bridge 禁止：

- 打开或复制 `callwarden.db`、`-wal`、`-shm`；
- 自己实现 task 状态更新；
- 在 daemon 不可用时写本地 SQLite；
- 接受客户端传入的 Windows SID 作为身份事实；
- 把 WSL 中的任意路径直接当成 Windows workspace 路径。

**实现状态（子任务2 已落地）**：

- `rust_ext/src/bin/cw_bridge.rs`（`cw-bridge` binary，Windows-only）：
  - 监听 loopback TCP（`CW_BRIDGE_ENDPOINT` 或 `127.0.0.1` 随机端口）；
  - `CW_BRIDGE_TOKEN_FILE`（默认 `~/.callwarden/bridge.token`）token 校验，
    失败返回 `E_BRIDGE_AUTH_FAILED`；
  - 请求帧 `recv_message` → 剥离 `bridge_token` → 经
    `daemon::client::windows::WindowsDaemonRpcClient` 转发到
    `\\.\pipe\callwarden-<sid>`；
  - daemon 不可达返回 `E_AUTHORITY_UNAVAILABLE`（recovery/fallback=forbidden）；
  - 远端业务错误保持结构化错误码（`DaemonRemoteError.code`）；
  - **不打开 SQLite、不实现 task 写逻辑**；
  - Rust 单测：token 校验 / 错误信封 / method-params 提取（`#[cfg(test)]`）。
- 测试：`tests/test_windows_bridge_e2e.py`（token / 错误信封 / method-params /
  源码契约约束断言，7 passed）。
- 非 Windows 平台 `cw-bridge` 打印错误退出（不冒充可用）。

**实现状态（子任务6 已落地）**：

- 测试：`tests/test_windows_wsl_authority_e2e.py` **4 passed**（Windows 平台真实 E2E）：
  - **8 并发源单胜者**：3 真实 CLI 子进程 + 3 真实 MCP 工具 + 2 个 bridge（WSL 模拟）
    客户端同时 claim 同一任务 → `task_events` 中 claimed 事件恰好 1 条（胜者），
    其余 7 个败者全部收到结构化 `task_conflict`，无 `database is locked`、无
    "daemon 不可用"伪语义（fail-closed，不伪装成连接失败）；
  - **WSL bridge 写 Windows authority**：bridge 客户端 `task.create` → Windows 侧
    `cw-client rpc task.status` 可读（同一 authority），WSL 侧无任何本地 DB 写入；
  - **bridge 重启幂等**：复用同一 `request_id` → daemon request dedup 返回已提交
    结果（同一 task_id），直读任务库 `task_events` 中 created 事件仅 1 条（无重复写）；
  - **authority pin fail-closed**：authority_id / task_db_fingerprint 不一致 →
    `DaemonRemoteError(E_AUTHORITY_MISMATCH)`，`mutation_call` 在 `verify_authority`
    处中断，不发起业务请求。
- 前置条件：默认 Named Pipe `\\.\pipe\callwarden-<sid>` 空闲时运行；被其他 daemon
  占用时按 P1-2 原则 skip（不杀他人进程，需空闲管道窗口真实执行）。

#### 过渡方案

在 bridge 二进制尚未实现前，允许 WSL 通过一个 Windows 侧命令执行器调用：

```bash
# 示例，具体 Windows Python 路径由安装器写入配置，不得硬编码为 C:\Python314
/mnt/c/<configured-python>/python.exe \
  /mnt/c/git_work/callwarden/cw.py task status <task_id>
```

这个方案只适合作为 CLI/task 证据回写的过渡通道。它必须调用 Windows 客户端的
daemon route，不能在 WSL Python 进程中 import `CodeGraphDB` 后打开 Windows DB。
生产部署应切换到 bridge，以支持完整 query、refresh 和 evidence RPC。

### 4.3 WSL 原生 authority

WSL 需要对 WSL ext4 中的项目独立工作时，可以运行 Linux daemon：

```text
WSL CLI/MCP/Agent
        │ UDS
        ▼
WSL cw-daemon
        │ 单写
        ▼
WSL ext4 ~/.callwarden/callwarden.db
```

约束：

- DB、WAL、SHM、CAS 和 staging 必须位于 WSL ext4 或 Linux 原生文件系统；
- UDS 默认使用 `/run/user/<uid>/callwarden/callwarden.sock`，开发环境可用
  `/tmp/callwarden-<uid>.sock`；
- WSL daemon 不得把 `CW_DAEMON_TASK_DB` 指向 `/mnt/c/...`；
- WSL daemon 管理的 workspace root 必须是 WSL 原生路径；
- WSL 原生 authority 的任务与 Windows authority 的任务默认不互相可见。

**实现状态（子任务7 已落地）**：

- 测试：`tests/test_wsl_local_daemon_e2e.py` **2 passed**（Windows 宿主经
  `wsl.exe -d ubuntu -- bash -s` 驱动 WSL Ubuntu，stdin 传脚本避免嵌套引号）：
  - **独立 authority**：WSL Linux daemon 使用 WSL ext4 临时根下独立的
    DB/WAL/SHM/CAS/UDS，authority_id 为 `linkplay-scm/linux/0/<fingerprint>`
    （独立 Linux authority），任务可独立创建/认领，Windows authority 全程不可达
    （fail-closed：不依赖、不回退 Windows 库，无任何 `/mnt/c` 写入）；
  - **重启幂等**：daemon 停止/重启（同一配置）后任务状态与事件完整保留
    （持久化而非内存态）。
- 环境注意：WSL 非交互 bash 不加载 `~/.bashrc`，需显式
  `export PATH="/root/.cargo/bin:$PATH"`；cargo target 与测试临时根置于 WSL ext4
  `/root/`（WSL 会话回收时会清理 `/tmp`）。

### 4.4 两个 daemon 同时运行

允许同时运行，但必须满足：

| 资源 | Windows authority | WSL authority |
|---|---|---|
| authority_id | 唯一 | 另一唯一值 |
| task DB | Windows 用户目录 | WSL ext4 用户目录 |
| registry/data root | Windows daemon 目录 | WSL daemon 目录 |
| endpoint | Named Pipe | Linux UDS |
| workspace 集合 | Windows workspace | WSL workspace |
| 写入关系 | 只写自身 authority | 只写自身 authority |

禁止把两个 daemon 配置成同一个 `task_db_path`、`registry_db_path`、`cas_db_path` 或
`codegraph_db_path_template`。启动时 daemon 必须检查 realpath/volume/authority
元数据，检测到共享路径后 fail-closed 并返回 `E_AUTHORITY_STORAGE_CONFLICT`。

**实现状态（子任务4 已落地）**：

- `rust_ext/src/daemon/config.rs` 的 `DaemonConfig` 新增：
  - `storage_paths()`：返回 `task_db` / `registry` / `data_root` / `codegraph` /
    `stage_toggle` 的规范化（realpath，不存在则 absolute）路径集合；
  - `validate_internal_storage()`：单 daemon 内部路径自冲突检测（`task_db` 与
    `registry` 等同路径报 `E_AUTHORITY_STORAGE_CONFLICT`）；
  - `validate_no_storage_overlap(&other)`：跨 authority 路径交集检测。
- `rust_ext/src/bin/cw_daemon.rs` 启动流程在 `ensure_directories` 之后调用
  `validate_internal_storage()`，冲突即退出（exit 1）。
- **跨 daemon 启动门禁**（评审二轮补强）：双 daemon 共存时，每个 daemon 启动必须
  通过 `CW_PEER_DAEMON_CONFIG=<peer 配置文件>` 显式声明对端配置；设置后启动时
  加载 peer 并调用 `validate_no_storage_overlap()`（含父子目录检测），冲突返回
  `E_AUTHORITY_STORAGE_CONFLICT` 并 exit 1。**部署门禁要求：启用双 daemon 共存时
  必须提供 `CW_PEER_DAEMON_CONFIG`；未提供时 daemon 正常启动（单 authority），
  但验收清单应确认双 daemon 场景配置了该变量**。
- 父子目录交集检测：`C:\data` 与 `C:\data\workspaces` 判冲突（`paths_conflict`）。
- Rust 单测：`config.rs::tests`（internal ok / task-registry 冲突 / no-overlap ok /
  shared-task-db 冲突 / 路径规范化）。
- Python 镜像检测测试：`tests/test_dual_daemon_storage_guard.py`（6 passed）。

> 说明：默认配置 `codegraph_db_path_template` 为空时，`resolve_codegraph_db_path`
> 回退到用户级单库 `~/.callwarden/callwarden.db`（两 authority 共享是设计使然），
> 不视为冲突；显式配置的路径才参与隔离校验。

## 5. 路由决策

### 5.1 配置优先级

建议新增显式 authority 配置，而不是继续扩大 `CW_DAEMON_MODE` 的含义：

```text
CW_AUTHORITY=auto|windows-host|wsl-local|linux-system
CW_DAEMON_TRANSPORT=auto|named-pipe|uds|windows-bridge|cli-bridge
CW_DAEMON_ENDPOINT=<transport-specific endpoint>
CW_DAEMON_AUTHORITY_ID=<optional pin; mismatch fails closed>
CW_BRIDGE_ENDPOINT=<WSL-visible bridge endpoint>
CW_BRIDGE_TOKEN_FILE=<ACL-protected token file>
```

推荐默认值：

- Windows 本地：`CW_AUTHORITY=auto` → 当前用户 Windows daemon；
- WSL 中访问 Windows workspace：工作区 registry 明确标记后选择
  `windows-host + windows-bridge`；
- WSL 原生 workspace：`wsl-local + uds`；
- 无法确定 authority：拒绝写操作，返回 `E_AUTHORITY_UNRESOLVED`。

**实现状态（子任务3 已落地）**：

- `config.py` 新增：
  - `get_daemon_authority()`：`CW_AUTHORITY`（auto/windows-host/wsl-local/linux-system），
    未配置时按平台默认（Windows→windows-host；WSL→wsl-local；其余→linux-system）；
  - `get_daemon_transport()`：`CW_DAEMON_TRANSPORT`（auto/named-pipe/uds/windows-bridge/cli-bridge）；
  - `get_bridge_endpoint()`：`CW_BRIDGE_ENDPOINT` / `CW_BRIDGE_ADDR`；
  - `resolve_daemon_endpoint_for_authority()`：按 authority+transport 解析 endpoint。
    规则：windows-host+windows-bridge→bridge；wsl-local/linux-system+uds→UDS；
    **windows-host + 非 Windows 无 bridge → 抛 `E_AUTHORITY_UNRESOLVED`
    （禁止直连 /mnt/c SQLite）**。
- `daemon_client.UnixDaemonRpcClient.__init__` 默认 endpoint 改用
  `resolve_daemon_endpoint_for_authority()`（不再直接 get_default_endpoint）。
- 测试：`tests/test_wsl_authority_routing.py` 9 passed（authority/transport 解析、
  bridge 路由、无 bridge 时 E_AUTHORITY_UNRESOLVED、WSL UDS、client 用 authority 解析器）。

### 5.2 按操作分类

| 操作 | Windows workspace | WSL workspace | authority 不可用 |
|---|---|---|---|
| task create/claim/report/apply/close | Windows daemon | WSL daemon | fail-closed |
| refresh/index/snapshot/CAS | Windows daemon | WSL daemon | fail-closed |
| governance/evidence/audit 写入 | Windows daemon | WSL daemon | fail-closed |
| 只读 query | 同 authority query | 同 authority query | 可返回明确 unavailable，不读另一库 |
| `CW_TASK_WRITE_POLICY=isolated` | 仅临时测试 | 仅临时测试 | 不得作为自动 fallback |

`auto` 只允许在同一 authority 的候选 transport 之间重试，例如 Windows bridge
临时不可达时重试 Windows CLI bridge；不能从 Windows authority 切换到 WSL authority。

### 5.3 连接握手

连接建立后，客户端必须先执行 `hello`/`ping`，响应至少包含：

```json
{
  "protocol_version": 1,
  "authority_id": "desktop-01/windows/user-.../db-...",
  "platform": "windows",
  "transport": "named-pipe",
  "task_db_fingerprint": "...",
  "workspace_capabilities": ["query", "refresh", "task-write"]
}
```

**实现状态（子任务1 已落地）**：

- `dispatch.rs` 的 `DaemonState` 增加 `authority_id` / `transport` / `task_db_fingerprint`；
  `handle_ping` 返回上述字段 + `protocol_version=1`。
- `authority_id_from_env()`：`CW_DAEMON_AUTHORITY_ID` 显式 pin 优先，否则派生
  `<hostname>/<platform>/<user>/<db-fingerprint>`。
- `transport_from_env()`：`CW_DAEMON_TRANSPORT` 优先，否则 `named-pipe`(Windows) / `uds`(其余)。
- `task_db_fingerprint_from_env()`：sha256(`CW_DAEMON_TASK_DB` realpath + size)，未配置时派生
  `$HOME/.callwarden/callwarden.db`，不存在/不可读返回空串（客户端视为不可写 authority）。
- Python `daemon_client.UnixDaemonRpcClient` 新增 `hello()`（提取 authority 字段）与
  `verify_authority(expected_authority_id, expected_fingerprint)`：mismatch 时抛
  `DaemonRemoteError("E_AUTHORITY_MISMATCH", ...)` 并 **fail-closed**（不继续请求、不写本地 DB）。
- 测试：`tests/test_daemon_authority_handshake.py`（hello 提取 / authority 缺失 fail-closed /
  authority mismatch / fingerprint mismatch / 匹配通过）。


客户端必须验证：

1. 返回的 authority 与 workspace registry 绑定一致；
2. 返回的 `task_db_fingerprint` 与当前任务上下文一致；
3. bridge 不得把 transport 标记为 WSL UDS；
4. 不一致时停止后续请求，不尝试另一份本地 DB。

## 6. 故障、重启与恢复

### 6.1 Windows daemon 不可用

Windows workspace 的所有治理写、task 写、refresh/index 写都返回结构化错误：

```text
E_AUTHORITY_UNAVAILABLE
authority=windows-host
recovery=start Windows cw-daemon or cw-bridge
fallback=forbidden
```

允许 bridge/客户端按有界窗口自动拉起 Windows daemon，但失败后必须停止；不得
打开 `/mnt/c/.../callwarden.db`，不得写 WSL DB。

### 6.2 WSL daemon 不可用

WSL workspace 的写请求同样 fail-closed。Windows daemon 不能接管 WSL workspace，
除非 workspace 已通过正式迁移流程重新绑定到 Windows authority。

### 6.3 bridge 重启

- bridge 重启不应导致 daemon 重启；
- 每个 RPC 使用 request_id，重试必须服从 daemon 的 request dedup；
- bridge 连接断开时，客户端可重连同一 authority；
- 任何“重连后 authority_id 改变”的响应必须被拒绝；
- 未确认提交结果的 mutation 不得盲目重复，必须先用 request_id 查询结果。

**实现状态（子任务5 已落地）**：

- `daemon_client.UnixDaemonRpcClient` 新增 `mutation_call()`：
  - 自动注入/复用 `request_id`（同一 mutation 重试复用同一 id，daemon 侧
    `TaskCollabStore.check_dedup` 返回已提交结果而非重复写入）；
  - 每次调用前 `verify_authority()` 做 authority pin 校验（authority_id /
    task_db_fingerprint mismatch → `E_AUTHORITY_MISMATCH` fail-closed）；
  - 连接失败时按 `reconnect_attempts` 重连（复用 request_id），耗尽后
    `DaemonUnavailableError`（fail-closed，不盲目重复）；
  - 远端业务错误（`DaemonRemoteError`）原样透传，不重试。
- `daemon_client.py` 补齐 `import time`（此前 `get_authoritative_clock` 已用但未导入）。
- 测试：`tests/test_bridge_restart_dedup.py` 6 passed（request_id 注入/复用/
  重试同 id / authority mismatch / 重连耗尽 / verify 调用）。

### 6.4 数据迁移

Windows authority 与 WSL authority 之间的 DB 迁移只能通过 daemon 提供的备份/恢复、
导出/导入或正式 workspace rebind 流程完成。禁止在 daemon 运行期间复制：

```text
callwarden.db
callwarden.db-wal
callwarden.db-shm
```

尤其禁止用 `immutable=1` 读取 Windows DB 作为“最新状态”；该连接可能跳过 WAL，
只能得到过期快照。

## 7. 安装与启动

### 7.1 Windows

安装器应部署：

1. `cw-daemon.exe`；
2. Windows 用户级启动项或按需 detached autostart；
3. `cw-bridge.exe`（WSL 共享功能启用时）；
4. ACL 仅当前用户可读的 bridge token；
5. authority manifest，记录 endpoint、authority_id、版本和数据根。

Windows daemon 与 bridge 必须分别有健康检查：

```text
cw daemon health             # daemon Named Pipe
cw daemon bridge             # bridge transport + downstream daemon 可达
```

**环境变量配置（子任务8 落地）**：

```text
# Windows daemon（Named Pipe）
CW_AUTHORITY=windows-host
CW_DAEMON_TRANSPORT=named-pipe          # 或 auto（Windows 默认 named-pipe）

# Windows bridge（WSL 共享功能启用时）
CW_BRIDGE_ENDPOINT=127.0.0.1:8456        # WSL 可达的 loopback TCP
CW_BRIDGE_TOKEN_FILE=C:\Users\<user>\.callwarden\bridge.token

# 可选 authority pin（客户端校验 daemon 身份）
CW_DAEMON_AUTHORITY_ID=desktop-01/windows/user-<sid>/db-<hash>
```

**安装检查清单**：

1. `cw-daemon.exe` 存在且可执行；Named Pipe 端点可被当前用户访问；
2. `cw-bridge.exe` 存在（若启用 WSL 共享）；
3. `bridge.token` 文件存在且 ACL 仅当前用户可读（拒绝 Everyone/Users 读）；
4. `cw daemon health` 返回 authority_id/transport/task_db_fingerprint；
5. `cw daemon bridge` 返回 bridge transport 与 downstream daemon 可达性（不可达 exit 1）。

### 7.2 WSL

WSL 安装器应提供两个互斥的 profile：

- `client`：只安装 WSL 客户端和 authority 配置，不安装本地 daemon；
- `local-daemon`：安装 WSL daemon、WSL ext4 数据根、UDS 和 systemd/user 启动项。

WSL `client` profile 默认不创建 `~/.callwarden/callwarden.db`，防止用户误以为
这是 Windows 权威库的副本；需要离线测试时只能使用显式 isolated profile。

**WSL client（访问 Windows authority）环境变量**：

```text
CW_AUTHORITY=windows-host
CW_DAEMON_TRANSPORT=windows-bridge       # 或 cli-bridge（过渡）
CW_BRIDGE_ENDPOINT=127.0.0.1:8456        # Windows bridge 的 loopback TCP
CW_BRIDGE_TOKEN_FILE=/mnt/c/Users/<user>/.callwarden/bridge.token
```

**WSL local-daemon（独立 authority）环境变量**：

```text
CW_AUTHORITY=wsl-local
CW_DAEMON_TRANSPORT=uds
CW_DAEMON_SOCKET=/tmp/callwarden-<uid>.sock   # 或 /run/user/<uid>/callwarden/callwarden.sock
CW_DAEMON_TASK_DB=/home/<user>/.callwarden/callwarden.db   # WSL ext4，禁止 /mnt/c
CW_DAEMON_DATA_ROOT=/home/<user>/.callwarden/daemon
CW_DAEMON_REGISTRY_DB=/home/<user>/.callwarden/daemon/registry.db
```

**WSL 启停与权限检查清单**：

1. `CW_AUTHORITY` 与目标 workspace 归属一致（WSL ext4 workspace → wsl-local；
   Windows workspace → windows-host）；
2. WSL 进程不执行 `sqlite3.connect('/mnt/c/.../callwarden.db')`；
3. WSL local-daemon 的 DB/WAL/SHM/CAS 全在 WSL ext4（`CW_DAEMON_TASK_DB` 不得指向
   `/mnt/c`）；
4. `python cw.py daemon health`（或 `cw daemon health`）返回 wsl-local authority；
5. 两个 daemon 同时运行时，`validate_no_storage_overlap` 通过（无
   `E_AUTHORITY_STORAGE_CONFLICT`）。

### 7.3 Linux 原生主机

继续使用 systemd service + UDS。Linux 主机的 authority 不应与 Windows/WSL 共享
SQLite 路径；跨机器访问应使用已认证的 TCP/mTLS bridge，而不是挂载数据库文件。

```text
CW_AUTHORITY=linux-system
CW_DAEMON_TRANSPORT=uds
CW_DAEMON_SOCKET=/run/callwarden/callwarden.sock
```

**启停命令**：`systemctl start callwarden-daemon` / `systemctl stop` / `systemctl status`
（health 应报告 authority_id/transport/task_db_fingerprint）。

## 8. 必须拆分的实现任务

本设计不宣称 bridge 已经实现。建议按以下小任务推进，每个任务独立 review：

| 顺序 | 任务 | 交付 | 关键门禁 |
|---:|---|---|---|
| 1 | Authority/transport handshake | `authority_id`、hello 响应、workspace binding | authority 不一致 fail-closed |
| 2 | Windows bridge MVP | loopback/WSL 可达端点到 Named Pipe 转发 | bridge 不读 DB、不改 RPC 语义 |
| 3 | WSL client routing | WSL `cw`/MCP 根据 workspace 选择 bridge 或 UDS | 不得回退 `/mnt/c` SQLite |
| 4 | 双 daemon storage guard | 启动检查 DB/registry/CAS realpath 冲突 | 冲突返回 `E_AUTHORITY_STORAGE_CONFLICT` |
| 5 | bridge 重启与 request dedup | 断线、重启、mutation 重试 | 无重复 task_event |
| 6 | Windows + WSL 真实 E2E | 2 CLI + MCP + WSL client 并发 claim/report | 单胜者、全记录在 Windows authority（✅ 已落地，见 §4.2） |
| 7 | WSL local-daemon E2E | WSL ext4 独立 workspace | 与 Windows DB realpath/authority 不同（✅ 已落地，见 §4.3） |
| 8 | 安装/运行文档 | Windows/WSL/Linux profiles、恢复命令 | 新用户不会被引导直连 DB |

## 9. 验收标准

### P0：绝不双写

1. WSL 进程无法通过项目公开客户端路径打开 Windows authority DB；共享模式下请求只
   经过 bridge/Windows daemon。
2. 人为让 bridge 和 daemon 不可用时，WSL task/refresh/write 返回
   `E_AUTHORITY_UNAVAILABLE`，本地 DB 写入计数为 0。
3. Windows daemon、bridge、WSL client 的日志都带 `authority_id`、transport 和
   request_id。

### P1：正确共享

1. Windows CLI、Windows MCP、WSL client 同时 claim 同一个 task，只有一个胜者；
   失败者均收到 `task_conflict`，所有事件在 Windows authority 可见。
2. bridge 重启后，已提交和未确认请求可用 request_id 正确区分，不出现重复 event。
3. Windows daemon 与 WSL daemon 同时运行时，双方 DB、WAL、SHM、CAS、registry
   路径没有交集。

### P2：可运维

1. `cw daemon health` 能报告 authority_id、transport、database fingerprint；
2. Windows/WSL daemon 可以分别停止和重启，互不影响；
3. 配置错误、authority 冲突、bridge token 错误、endpoint 不可用都有稳定错误码；
4. 安装器能明确区分 `client` 和 `local-daemon` profile。

**稳定错误码（子任务1 落地）**：
- `E_AUTHORITY_MISMATCH`：客户端 `verify_authority` 检测到 authority_id 或
  task_db_fingerprint 与期望不一致时抛出（fail-closed，不继续请求、不写本地 DB）。
- `E_AUTHORITY_STORAGE_CONFLICT`（子任务4）：双 daemon 启动时检测到共享
  task/registry/CAS/codegraph/staging 路径。
- `E_AUTHORITY_UNAVAILABLE`：authority daemon 不可达时写请求的结构化错误。
- `E_AUTHORITY_UNRESOLVED`：无法确定 workspace 归属的 authority。

## 10. 当前问题的直接处理建议

在 bridge 实现前，立即采用以下操作规则：

1. Windows agent/MCP 通过现有 Windows daemon 工作；保持
   `CW_DAEMON_MODE=auto`、`CW_TASK_WRITE_POLICY=shared`。
2. WSL agent 不执行 `sqlite3.connect('/mnt/c/.../callwarden.db')`，也不运行
   `cw task` 的本地 Python DB fallback。
3. WSL 需要写 Windows 任务时，由 Windows 侧 agent 执行 `cw task ...`，或使用
   配置好的 Windows `cw.exe`/Python CLI bridge；不能由 WSL 直接导入 `CodeGraphDB`。
4. WSL 需要独立测试时，将 `HOME`、`CW_DAEMON_TASK_DB`、`CW_DAEMON_DATA_ROOT`
   和 `CW_DAEMON_SOCKET` 全部指向 WSL ext4 临时根，并使用 WSL daemon。
5. A4 的 UID/owner 测试应在 WSL ext4（如 `/tmp/callwarden-a4`）执行；不要把
   `/mnt/c` drvfs 的 `st_uid=0` 现象当作生产 ACL 语义，也不要用放宽 root owner
   检查来掩盖测试文件系统限制。

## 11. 不在本任务范围内

- 本文不实现 Windows bridge；
- 本文不改变现有 Named Pipe/UDS framing；
- 本文不允许修改历史 G0 证据或直接补写其他任务归属；
- 本文不定义跨机器多租户 TCP/mTLS 的完整协议；
- 本文不把两个 authority 自动合并为一个全局任务库。

