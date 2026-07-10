# 企业级架构演进计划：从单机工具到工业级基础设施

## 背景

### 当前架构（单机版）

Call Warden 当前是"每个工作区一个独立 SQLite 数据库"的单机工具：

- 数据库路径：`$HOME/.callwarden/<16位hash>/callwarden.db`
- hash 是项目根路径绝对路径的 SHA-256 前 16 位
- 每个工作区独立解析、独立存储、独立查询
- CLI/MCP Server 都在用户进程内运行

### 目标场景（工业级部署）

将 Call Warden 部署到典型的企业级共享开发环境：

**硬件/操作系统**：
- 一台 Linux 开发机，十几个开发人员共享
- Ubuntu 14.04 / 16.04 / 18.04 / 20.04 / 22.04 / 24.04 多容器并存
- 容器的 `/opt` 和 `/home` 挂载在宿主机上
- 部分开发人员通过 SMB 访问自己的用户目录
- 部分通过 VSCode Remote 访问自己的用户目录

**用户工作模式**：
- 每个开发人员有 3-10 个本地工作区（同一 repo 的不同分支）
- `/opt` 下安装着不同厂家的工具链（永远不变）
- 95% 以上的代码文件在不同工作区间是重复的，只是进度不一致

### 单机架构在这种环境下的灾难

如果继续用"每个工作区一个 SQLite"的方案：

1. **I/O 与 CPU 踩踏**：每天早上大家同时 `git pull` 或切换分支时，十几个大内存进程同时启动，疯狂扫描相同的文件，机器 CPU 和磁盘 IO 瞬间被打满。
2. **存储爆炸**：一个完整 firmware 数据库假设 1GB，10 人 × 每人 5 个分支 = 50GB，其中 95%+ 是重复数据。
3. **重复解析**：相同文件在不同工作区被重复解析（每次约 0.5-2 秒），浪费 95% 的算力。
4. **路径冲突**：容器 A 的 `/home/user1/work/firmware` 在宿主机是 `/data/docker_volumes/user1/work/firmware`，通过 SMB 又变成 `Z:\work\firmware`。同一个文件三种路径，无法跨工作区共享数据。

---

## 架构重构方案：三层存储 + 单例守护进程

### 核心思想

引入 CAS（Content-Addressable Storage，内容寻址存储）—— Git 底层、Bazel、ccache 等现代工具极速运行的核心思想。文件内容相同则共享解析结果，避免重复工作。

### 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│                     宿主机 Linux                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │       Call Warden Daemon (单例，systemd 服务)          │  │
│  │       资源限制：4GB 内存 / 4 CPU 核心                   │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Layer 1: 全局 CAS 缓存池                        │  │  │
│  │  │  /var/lib/callwarden/cas.db (daemon-only, 0600)│  │  │
│  │  │  主键：sha256(content + lang + parser_version)   │  │  │
│  │  │  存储：AST / 符号表 / 单文件内 raw calls（内容寻址）│  │  │
│  │  │  ⚠ 不存跨文件 call_edges（依赖 build context）     │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Layer 2: 工具链专属库 (System/Toolchain DB)      │  │  │
│  │  │  /var/lib/callwarden/toolchain.db (只读)         │  │  │
│  │  │  一次性扫描 /opt 下的头文件和静态库                │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Layer 3: 瘦工作区索引 (Thin Workspace DB)        │  │  │
│  │  │  $HOME/.callwarden/<workspace_fingerprint>/       │  │  │
│  │  │  存储：file_path → content_hash 映射 + 调用边      │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │           ↑ Unix Domain Socket (UDS)                  │  │
│  └───────────┼───────────────────────────────────────────┘  │
│              │                                              │
│  ┌───────────┼──────────────────────────────────────────┐   │
│  │   容器 A (Ubuntu 14.04)  容器 B (Ubuntu 22.04)  ...  │   │
│  │   user1 通过 VSCode      user2 通过 SMB             │   │
│  │   /home/user1/work/      Z:\work\                    │   │
│  │       ↓                       ↓                      │   │
│  │   cw CLI → UDS            cw CLI → UDS               │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## 三层存储设计

### Layer 1: 全局 CAS 缓存池

**目标**：消灭 95%+ 的重复文件解析。

**位置**：`/var/lib/callwarden/cas.db`（宿主机共享目录，daemon-only 0600）

> **v5 P1 修复（权限隔离）**：Global CAS 文件权限改为 **daemon-only（0600）**，客户端**不直接打开 SQLite**。CAS 包含跨用户项目的符号正文，所有用户只读仍会造成跨项目源码泄露。企业模式下客户端只能通过 **UDS API + workspace/snapshot ACL** 查询，daemon 负责实际文件读写。单用户模式下 Local CAS（`~/.callwarden/cas.db`）保持当前用户可读写，不涉及跨用户泄露。

**主键设计**（关键工程细节）：

```
hash_key = sha256(
    file_content +
    language +           # rust / c / python / ...
    parser_version +     # tree-sitter-c v0.20 vs v0.22 的 AST 不同
    callwarden_version   # Call Warden 自身升级后旧 AST 失效
)
```

**为什么不能只 hash 文件内容**：同一个 `main.c`，用 tree-sitter-c v0.20 解析的 AST 和 v0.22 解析的 AST 不一样。如果只 hash 内容，升级 parser 后全局缓存里的旧 AST 会让查询结果不一致。

**存储内容（关键：只存"单文件粒度"的可共享结果，不存跨文件解析产物）**：
- `file_cache` 表：`hash_key → AST 序列化 / 符号表 / 解析时间`
- `symbol_cache` 表：`hash_key → symbol_id / symbol_name / symbol_type / location`
- `raw_calls_in_file` 表：`hash_key → (caller_symbol_offset, callee_name, callee_qualifier)` —— 单文件内部 tree-sitter 能直接解出的 raw 调用文本（如 `foo()`、`std::vector::push_back`），**未跨文件解析**
- 查询命中：输入文件算 hash → 查 `file_cache` → 直接返回符号表 + raw 调用，跳过 tree-sitter parse

**为什么不存跨文件 `call_edges`**：
跨文件调用边（如 `a.c::main` → `b.c::helper`）的解析依赖 build context —— include path、macro 定义、链接配置、同一符号的多重声明、sysroot 选择都不同时，`main` 实际指向的 `helper` 也不同。直接按单文件 hash 全局共享会让 user1 的 sysroot A 下的边错误地命中 user2 的 sysroot B 下的边。因此跨文件 `call_edges` 必须在 workspace/snapshot 层（见 Layer 3）解析后归属，**不进 CAS**。CAS 只提供单文件 raw calls 作为解析输入。

**收益**：相同 commit 的相同文件，10 个开发人员只需解析一次。

### Layer 2: 工具链专属库

**目标**：`/opt` 下的工具链一次扫描终身受益，但必须按真实指纹隔离不同厂家/版本/sysroot 的工具链，不能"一刀切"。

**位置**：`/var/lib/callwarden/toolchain.db`

**关键修正：工具链也必须做 fingerprint**。

之前文档假设"`/opt` 永远不变，所以不需要 hash" —— 这在固件环境里是错的。`/opt` 实际是多厂家、多版本、多 sysroot、多 target triple、多 include path 并存。把所有头文件扫进一个全局库会导致 `printf` 在 `arm-none-eabi-gcc 9.3` 和 `aarch64-linux-gnu-gcc 12` 下指向不同声明，却错误地命中同一个 CAS 条目。

**`toolchain_fingerprint` 设计**：

```python
toolchain_fingerprint = sha256(
    toolchain_root_realpath +     # realpath 解析后的真实路径（防 symlink 误判）
    compiler_version +            # gcc 9.3.1 / clang 16.0.0
    target_triple +               # arm-none-eabi / aarch64-linux-gnu
    sysroot_path +                # --sysroot=/opt/arm/sysroot-a53
    include_dirs +                # -I 列表（顺序敏感）
    predefined_macros +           # gcc -dM -E 预定义宏（__ARM_ARCH=7 等）
    language_standard +           # -std=c++17 / -std=c11
    parser_version +              # tree-sitter-c 版本
    callwarden_version
)
```

**特点**：
- 按 `toolchain_fingerprint` 分桶存储，不同 sysroot 的符号互不污染
- 完全只读
- 通过 SQLite `ATTACH DATABASE` 挂载到瘦工作区
- 查询外部符号时 daemon 根据 workspace 的 build context 选择对应桶

**初始化**：管理员运行 `cw daemon register-toolchain --path /opt/toolchain_a --target arm-none-eabi --sysroot /opt/arm/sysroot-a53`，daemon 计算 fingerprint 后扫描入库。新增 sysroot 时显式注册新桶，而非覆盖。

**workspace 与 toolchain 的绑定**：workspace 注册时声明其使用的 `toolchain_fingerprint`（或通过 build context 自动探测），daemon 解析时强制走对应桶。

### Layer 3: 瘦工作区索引

**目标**：每个开发者工作区对应的 thin DB 极小（几 MB），由 daemon 集中持有，不再放回客户端本地。

**位置**：`/var/lib/callwarden/workspaces/<workspace_instance_id>/thin.db`（集中存储，daemon 拥有；客户端本地不再持有 SQLite）

**关键修正：拆分 `workspace_instance_id` 与 `snapshot_id` 两个概念**。

之前文档只用 `workspace_fingerprint = sha256(remote + HEAD)` 一个值，会把"同一 commit 的多个本地工作区"合并成同一 thin DB。这忽略了实际工作区常有：未提交修改、staged 文件、untracked 文件、不同 submodule 检出状态、不同 sparse checkout、不同 ignore 规则 —— 这些都不是 HEAD 能反映的。

**两个独立概念**：

```python
# workspace_instance_id: 真实本地工作区身份（私有，不共享）
workspace_instance_id = sha256(
    owner_uid +                # 谁的工作区
    host_real_root +           # 宿主机真实根目录（realpath 解析后）
    git_remote_url +           # 同一 repo
    git_head_commit_sha +      # 同一分支的 commit
    submodule_state_hash +     # submodule 检出状态（git submodule status 的 hash）
    sparse_checkout_hash +    # sparse-checkout 配置 hash
    working_tree_dirty_hash   # 是否有未提交修改（不为空 → 不进 snapshot 共享）
)

# snapshot_id: 可跨用户共享的代码快照身份（公共，可复用）
snapshot_id = sha256(
    git_remote_url +
    git_head_commit_sha +
    submodule_state_hash +
    sparse_checkout_hash +
    toolchain_fingerprint     # 同一 commit 不同 sysroot 仍不是同一 snapshot
)
```

**复用规则**：

| 状态 | workspace_instance_id | thin DB 来源 |
|------|----------------------|-------------|
| clean working tree（无 staged/未提交修改） | 仍唯一 | **复用 snapshot 级 thin DB**（daemon-only 共享） |
| dirty working tree | 唯一 | **独立 thin DB**（不复用，因为 dirty 内容无 snapshot_id） |
| staged 但未 commit | 唯一 | 独立 thin DB（staged 内容算 `working_tree_dirty_hash`） |

**实现要点**：
- clean 工作区：daemon 把 `workspace_instance_id` 标记为"指向 `snapshot_id` 的视图"，thin DB 物理上只存一份；workspace_meta 表里只记一条 `(workspace_instance_id, snapshot_id, owner_uid)` 映射
- dirty 工作区：daemon 为该 `workspace_instance_id` 单独建一份 thin DB（或基于 `snapshot_id` 做 copy-on-write diff 表）
- thin DB 中 `file_path_map` 仍是 `relative_path → content_hash`，dirty 的文件覆盖 snapshot 的版本
- 跨用户共享发生在 `snapshot_id` 层（clean 工作区之间），不在 `workspace_instance_id` 层

这样既保留"同一 commit 多人共享 CAS 解析结果"，又不把 user1 的 dirty 修改错误地泄漏给 user2。

**存储内容**：
- `file_path_map` 表：`relative_path → content_hash`（核心映射）
- `call_edges` 表：`(caller_symbol_ref, callee_symbol_ref, build_context_id)` —— 跨文件解析后的调用边，**这是工作区/snapshot 独有的，不进 CAS**
- `workspace_meta` 表：`workspace_instance_id / snapshot_id / owner_uid / git_remote / git_head / client_view_root / host_real_root`

**关键设计选择：调用关系的边存在哪一层？**

边是"哪个符号调用了哪个符号"。**必须区分两种边**：

| 边类型 | 来源 | 依赖 build context | 归属层 |
|------|------|------------------|--------|
| **raw calls**（单文件内文本调用） | tree-sitter 直接从单文件解出 | 否 | **CAS 库**（`raw_calls_in_file` 表，按 file_hash 索引） |
| **resolved call_edges**（跨文件解析边） | 用 build context 把 raw calls 解析到具体目标符号 | **是** | **workspace / snapshot**（`call_edges` 表） |

跨文件 `call_edges` 必须挂在 workspace/snapshot 上，因为：
- `main.c::main → helper()` 的 raw call 来自 CAS（按 file_hash）
- 但 `helper` 到底指向 `a.c::helper` 还是 `b.c::helper`，取决于 `-I./src/a` 还是 `-I./src/b`，这是 build context 决定的
- 不同 sysroot、不同 include path、不同宏配置 → 同一 raw call 解析到不同目标 → 边不能全局共享

**跨分支共享什么**：相同 `file_hash` 的文件，`raw_calls_in_file` 只算一次（CAS 命中）。`call_edges` 每个 workspace/snapshot 独立解析。

**symbol_id 设计**：`symbol_id = hash(file) + symbol_offset`，跨分支同一文件同一符号 id 相同，CAS 命中后边可直接复用上游 CAS 的 symbol_id 引用。

---

## 进程模型：单例守护进程

### 为什么需要 Daemon

不要让每个开发者的 VSCode 或终端拉起解析进程，会导致：
- 资源竞争：十几个进程抢占 CPU/内存
- 写锁冲突：多个进程同时写 CAS 库
- 重复工作：相同文件被多个进程同时解析

### Daemon 设计

**部署方式**：systemd 服务，开机自启

```ini
# /etc/systemd/system/call-warden-daemon.service
[Unit]
Description=Call Warden Daemon
After=network.target

[Service]
Type=simple
ExecStart=/opt/callwarden/bin/cw daemon start
Restart=on-failure
# 资源限制
MemoryMax=4G
CPUQuota=400%
# 用户隔离
User=callwarden
Group=callwarden

[Install]
WantedBy=multi-user.target
```

**通信机制**：Unix Domain Socket (UDS) 为主，本地 TCP 仅作为容器穿透的可选通路

- **UDS 路径**：`/var/run/callwarden.sock`（权限 `0660`，属主 `callwarden:callwarden`，组成员可连）
- **本地 TCP**：默认 **关闭**。只在容器无法挂 UDS 时开启，且必须启用 mTLS + per-container token
- 协议：JSON-RPC 2.0（与 MCP 兼容）

### 权限模型（关键工程细节，企业部署不能省）

之前文档把"权限隔离"仅列为风险条目，没有设计。多人共享机器下这是核心安全控制。

**1. UDS 调用方身份识别：`SO_PEERCRED`**

UDS 接受连接后立即读 `SO_PEERCRED`（Linux）/ `LOCAL_PEERCRED`（BSD/macOS），拿到调用方 `uid / gid / pid`，记入请求上下文：

```python
import struct, socket

def get_peer_cred(conn: socket.socket) -> tuple[int, int, int]:
    # Linux SO_PEERCRED
    cred = conn.getsockopt(socket.SOL_SOCKET, 17, struct.calcsize('iII'))
    pid, uid, gid = struct.unpack('iII', cred)
    return pid, uid, gid
```

**2. Workspace 与 owner UID 绑定**

- workspace 注册时记录 `owner_uid`（来自 `SO_PEERCRED`）
- 所有后续查询/修改必须校验：`request.uid == workspace.owner_uid`（或调用方在 `callwarden` admin 组）
- daemon 拒绝任何"workspace_root 路径"形式的越权读取（user1 不能通过传 user2 的路径查 user2 的内容）

**3. TCP 通路必须启用强认证**

- **禁用裸 TCP**：无认证的 `127.0.0.1:8080` 在多容器环境下任意容器内进程都能连，等于无认证
- **必须 mTLS**：daemon 持服务端证书，每个容器持客户端证书（CN = container_id），daemon 校验证书 + 容器白名单
- **per-container token**：每个容器分配独立 token，daemon 校验 `Authorization: Bearer <token>` 后才接受 workspace_root 参数
- **token 与 UID 绑定**：token 注册时绑定宿主机 UID，TCP 路径下用 token → uid 映射代替 `SO_PEERCRED`

**4. 权限分级**

| 操作 | 普通用户（owner） | 普通用户（非 owner） | admin 组 |
|------|------------------|---------------------|----------|
| 查询自己的 workspace | ✓ | ✗ | ✓ |
| 注册自己的 workspace | ✓ | ✗ | ✓ |
| 刷新 / 解析 | ✓（仅 owner） | ✗ | ✓ |
| 注册 toolchain | ✗ | ✗ | ✓ |
| 删除他人 workspace | ✗ | ✗ | ✓（审计） |

**5. 审计日志**：daemon 记录所有 `workspace_root` / `relative_path` / `owner_uid` / `peer_uid` 的查询路径，便于事后追溯越权尝试。

**并发模型：Job Deduplication（不是请求队列）**

请求 key = `(content_hash, operation_type)`：
1. 第一个请求进来，计算中
2. 第二个相同 key 请求进来，挂在第一个请求的 future 上
3. 第一个完成，所有挂载的请求同时返回

比"请求队列"更高效，因为不同开发者的不同分支如果同时改了同一个文件（hash 相同），只需算一次。

### 客户端模式

CLI 和 MCP Server 改为"客户端模式"：
- 检测 UDS 是否可用 → 可用：发送请求到 Daemon
- UDS 不可用 → 回退到本地单机模式（向后兼容）

---

## 容器与宿主机的"路径穿越"问题

### 问题描述

同一个文件，在不同视角下路径不同：
- 容器 A：`/home/user1/work/firmware/main.c`
- 宿主机：`/data/docker_volumes/user1/work/firmware/main.c`
- SMB 挂载：`Z:\work\firmware\main.c`

### 解决方案

**所有存储路径必须使用相对路径**：

```python
# 错误：存绝对路径
db.insert_file("/home/user1/work/firmware/main.c")  # 容器 A 视角

# 正确：存相对路径
db.insert_file("src/main.c")  # 相对于 workspace_root
```

**关键修正：客户端不能直接传 `workspace_root` 让 daemon 任意访问文件**。

原方案"客户端传 `workspace_root`，daemon 读文件算 hash"在多人共享机器上是越权入口 —— user1 可以构造 `workspace_root=/home/user2/secret` 让 daemon 用 `callwarden` 用户身份读取 user2 的文件。必须把"客户端视角的路径"和"daemon 真实可访问的路径"分离，并加上注册 + 校验。

### Workspace 注册机制（可信根映射）

**注册时四元组绑定**：

```python
{
    "owner_uid": 1001,                 # 来自 SO_PEERCRED，daemon 自动填，客户端不可指定
    "client_view_root": "/home/user1/work/firmware",       # 客户端视角（容器内路径）
    "host_real_root": "/data/docker_volumes/user1/work/firmware",  # 宿主机真实路径
    "repo_id": "github.com/foo/bar.git@abc123",            # git remote + HEAD
}
```

- `client_view_root` 由客户端提交（用于显示和回查相对路径）
- `host_real_root` 由 daemon 在注册时用 `realpath` 解析得到（客户端不可直接指定）
- 注册后 daemon 生成 `workspace_instance_id`（见 Layer 3）并返回给客户端

**查询接口改为引用 `workspace_instance_id`**：

```python
# 客户端只传 workspace_instance_id（注册时拿到的 token）+ 相对路径
client.get_callers(
    workspace_instance_id="ws_abc123...",   # 不再传 workspace_root
    relative_path="src/main.c",
    symbol_name="main",
)
```

Daemon 收到查询后执行**路径校验链**：

```python
def resolve_safe(ws: Workspace, relative_path: str) -> str:
    # 1. owner 校验：peer_uid 必须 == ws.owner_uid 或在 admin 组
    assert_peer_owns_workspace(peer_uid, ws)

    # 2. 路径规范化：去掉 .. 和多余分隔符
    rel = os.path.normpath(relative_path).replace("\\", "/")
    if rel.startswith("/") or ".." in rel.split("/"):
        raise PermissionError("relative_path 不能含绝对路径或 .. 跳层")

    # 3. 拼接真实根
    real_path = os.path.realpath(os.path.join(ws.host_real_root, rel))

    # 4. 前缀校验：real_path 必须在 host_real_root 之下（防 symlink 逃逸）
    if not real_path.startswith(ws.host_real_root + os.sep) and real_path != ws.host_real_root:
        raise PermissionError(f"路径逃逸：{real_path} 不在 {ws.host_real_root} 下")

    # 5. symlink 检查：相对路径上任何一段是 symlink 都拒绝（或记录审计）
    check_no_symlink_in_relpath(ws.host_real_root, rel)

    # 6. owner 一致性：文件 owner_uid 仍属 ws.owner_uid（防宿主机被改后越权）
    stat = os.stat(real_path)
    if stat.st_uid != ws.owner_uid:
        log_audit("owner_mismatch", ws, real_path, stat.st_uid)
        raise PermissionError("文件 owner 与 workspace owner 不一致")

    return real_path
```

**容器路径映射**：daemon 启动时读取 `/etc/callwarden/container_mounts.yaml`（admin 维护），把 `client_view_root` 映射到 `host_real_root`：

```yaml
mappings:
  - container: ubuntu_2204
    client_prefix: "/home"
    host_prefix: "/data/docker_volumes"
  - smb_share: "Z:"
    client_prefix: "Z:/"
    host_prefix: "/smb_shares/user1/"
```

daemon 拒绝任何未在 mappings 中声明的路径前缀，避免用户随意指定宿主机路径。

这样无论开发者从哪个容器、哪种协议挂载进来：
1. 必须先注册 workspace（拿到 `workspace_instance_id`）
2. 所有后续查询引用 `workspace_instance_id`，不能直接传 `workspace_root`
3. daemon 内部做 realpath + 前缀 + symlink + owner 校验
4. SMB / 容器 / 宿主机路径通过 admin 维护的映射表转换

---

## 内存表与 SQLite 的取舍

### 用户偏好

用户之前偏好"内存表 + SQLite dump"（Daemon 进程内存表，crash 后从 SQLite 重建）。

### 重新评估

在 Daemon 场景下：
- **Daemon 进程内存表**：查询极快（μs 级），但 crash 后丢失，需要从 SQLite 重建
- **SQLite WAL 模式**：原生支持多 reader + 1 writer，crash 不丢数据，查询比内存慢 5-10 倍但可接受

### 推荐方案：内存索引 + 磁盘内容

**Daemon 启动时把 CAS 索引（不是内容）加载到内存表，内容仍在 SQLite**。

- 索引小：10M 文件 ≈ 1GB 索引（hash + 元数据）
- 内容大：10M 文件 ≈ 50GB 内容（AST + 符号表）

这样查询 90% 命中内存，miss 才查 SQLite。

**内存表结构**：
```python
# Python dict，启动时从 SQLite 加载
_hash_index: Dict[str, FileMeta] = {
    "abc123...": FileMeta(
        file_size=12345,
        language="c",
        parser_version="0.22",
        symbol_count=42,
        sqlite_rowid=67890,  # 用于回查 SQLite 获取详细数据
    ),
}
```

**Crash 恢复**：Daemon 启动时从 SQLite 重建 `_hash_index`，10M 文件约需 30 秒。

---

## 迁移路径（分 3 阶段）

> **⚠️ 本节已废弃**：下方的 3 阶段迁移路径已被 [enterprise-phase1-phase3-detail.md](enterprise-phase1-phase3-detail.md) v4 取代。
> v4 的实施顺序为：Phase 1（Rust 多语言 parse）→ Phase 3A（Local CAS，per-UID `~/.callwarden/cas.db`）→ Phase 2 daemon → Phase 3B（daemon 内单源实现）。
> 本节保留作为历史背景参考，不再代表当前实施计划。具体差异：
> - v4 的 CAS 是 per-UID Local DB（`~/.callwarden/cas.db`），不是 `/var/lib/callwarden/cas.db`
> - v4 的 Phase 1 是 Rust 多语言 parse 接入，不是 CAS
> - v4 的 Phase 3A 在 Phase 2 daemon 之前实施（Local CAS 不依赖 daemon）
> - v4 的 Phase 3B（查询路径迁移）延后到 Phase 2 daemon 之后

不能一次推翻现有架构，因为：
- 单机开发者仍需要零依赖的本地工具
- 企业部署初期，Daemon 还没稳定时有 fallback

### 阶段 1：CAS 全局缓存（核心收益）

> **⚠️ 已废弃**：v4 中此阶段拆分为 Phase 1（Rust 多语言 parse）和 Phase 3A（Local CAS，per-UID DB）。
> CAS 路径从 `/var/lib/callwarden/cas.db` 改为 `~/.callwarden/cas.db`，且 CAS 表为自包含（含 `cas_symbol_contents`）。

**目标**：抽取文件 hash 索引层，多分支共享 AST/符号。

**改动范围**：
- 新增 `db/db_cas.py`：CAS 存储层（file_hash → AST/symbols）
- 修改 `db_build.py`：解析文件前先查 CAS，命中则跳过 tree-sitter parse
- 配置项：`callwarden.cas.enabled = true`、`callwarden.cas.db_path = /var/lib/callwarden/cas.db`

**预期收益**：
- 95% 重复文件解析被消灭
- 同一台机器上的不同分支共享 CAS（用户级，跨用户共享需要阶段 2）

**风险**：
- 数据库主键从"文件路径"改为"文件 hash"是架构性破坏改动，影响所有 Mixin
- 需要保持向后兼容：无 CAS 时回退到当前的"路径→内容"模式

**实施工作量**：约 1-2 周

### 阶段 2：Daemon + UDS（多用户协同）

**目标**：CLI/MCP 改为客户端模式，单例 daemon 调度。

**改动范围**：
- 新增 `server/daemon.py`：常驻进程 + UDS 监听 + Job Dedup
- 修改 `cli/main.py`：检测 UDS 可用 → 发送请求；不可用 → 本地模式
- 修改 `server/mcp_server.py`：改为 thin client，转发到 Daemon

**预期收益**：
- 多用户共享 CAS（跨用户、跨容器）
- Job Dedup 消除并发重复解析
- 资源受控（systemd 限制）

**风险**：
- Daemon crash 影响所有用户（需要健壮的 crash 恢复）
- 权限模型必须在阶段 2 一并落地（`SO_PEERCRED` + owner UID 绑定 + TCP mTLS），不能拖到阶段 3

**实施工作量**：约 2-3 周

### 阶段 3：工具链库 + 路径穿越（企业级完善）

**目标**：`/opt` 工具链只读库 + 容器路径穿越支持。

**改动范围**：
- 新增 `cw daemon register-toolchain`：按 `toolchain_fingerprint` 分桶扫描工具链（含 sysroot/include/宏/语言标准）
- 修改所有路径处理：绝对路径 → 相对路径 + `workspace_instance_id`
- 实现 workspace 注册机制（owner_uid + client_view_root + host_real_root + repo_id 四元组）
- 实现路径校验链（realpath + 前缀校验 + symlink 检查 + owner 一致性）
- 拆分 `workspace_instance_id`（私有）与 `snapshot_id`（公共可复用）

**预期收益**：
- 工具链符号按指纹分桶，避免不同 sysroot 的符号互染
- 容器/SMB/VSCode 三种访问方式通过可信根映射统一
- clean 工作区跨用户共享 snapshot 级 thin DB，dirty 工作区独立隔离

**风险**：
- 路径改造影响面广，需要逐个 Mixin 修改
- `snapshot_id` 依赖 git，无 git 项目需要 fallback（manifest hash 兜底）
- workspace 注册对 CI/CD 自动化场景需要无交互 API

**实施工作量**：约 1-2 周

---

## 关键工程细节汇总

### 1. CAS Hash 必须包含 parser 版本

```python
hash_key = sha256(
    file_content +
    language +
    parser_version +      # tree-sitter-c v0.20 vs v0.22
    callwarden_version    # Call Warden 自身升级
)
```

只 hash 文件内容不够：同一个 `main.c`，不同 parser 版本解析的 AST 不同。

### 2. Workspace 身份拆分：instance_id + snapshot_id

```python
# 错误：单一 fingerprint（会把 dirty 工作区和 clean 工作区错误合并）
workspace_fingerprint = sha256(remote + HEAD)

# 正确：两个独立概念
workspace_instance_id = sha256(owner_uid + host_real_root + remote + HEAD
                               + submodule_state + sparse_checkout + dirty_hash)
snapshot_id           = sha256(remote + HEAD + submodule_state
                               + sparse_checkout + toolchain_fingerprint)
```

- `workspace_instance_id`：私有，每个真实本地工作区唯一，不可跨用户共享
- `snapshot_id`：公共，clean 工作区之间可复用同一 thin DB
- dirty 工作区无 `snapshot_id`，daemon 单独建 thin DB（或基于 snapshot 做 COW diff）

### 3. Toolchain 必须做 fingerprint（不能"一刀切"）

```python
toolchain_fingerprint = sha256(
    toolchain_root_realpath + compiler_version + target_triple +
    sysroot_path + include_dirs + predefined_macros +
    language_standard + parser_version + callwarden_version
)
```

`/opt` 在固件环境是多厂家、多 sysroot、多 target triple，不分桶会导致同一 `printf` 在 ARM GCC 9 和 aarch64 GCC 12 下命中同一 CAS 条目。

### 4. Job Deduplication 而非请求队列

```python
# 请求 key = (content_hash, operation_type)
pending_jobs: Dict[str, Future] = {}

def submit_job(content_hash, op_type, payload):
    key = f"{content_hash}:{op_type}"
    if key in pending_jobs:
        return pending_jobs[key]  # 挂载到现有 future
    future = executor.submit(do_work, payload)
    pending_jobs[key] = future
    future.add_done_callback(lambda f: pending_jobs.pop(key, None))
    return future
```

### 5. 内存索引 + 磁盘内容

- 内存：`_hash_index: Dict[hash, FileMeta]`（10M 文件 ≈ 1GB）
- 磁盘：SQLite 存完整 AST/符号表（10M 文件 ≈ 50GB）
- 查询：内存命中 → 回查 SQLite 取详情

### 6. 调用关系：raw calls 进 CAS，resolved edges 进 workspace

| 边类型 | 来源 | 归属层 |
|------|------|--------|
| `raw_calls_in_file`（单文件内文本调用） | tree-sitter 单文件解析 | **CAS**（按 file_hash 索引） |
| `call_edges`（跨文件解析边） | build context 解析 raw calls 到目标符号 | **workspace / snapshot** |

- 节点（符号）→ CAS 库（按 file_hash 索引）
- 跨文件 `call_edges` → workspace/snapshot（边引用两端 symbol_id）
- `symbol_id = hash(file) + symbol_offset`
- 跨分支共享：相同 file_hash 的 raw calls 只算一次；call_edges 每个 workspace 独立解析

### 7. 权限模型：UDS SO_PEERCRED + owner UID 绑定

```python
# UDS 接受连接后立即识别调用方
pid, uid, gid = get_peer_cred(conn)  # Linux SO_PEERCRED

# workspace 与 owner UID 绑定，所有查询校验
assert request.uid == workspace.owner_uid or in_admin_group(request.uid)

# TCP 通路必须 mTLS + per-container token，禁用裸 TCP
```

### 8. 路径可信根映射（不能让客户端直接传 workspace_root）

```python
# 存储：相对路径 + content_hash
db.insert_file(relative_path="src/main.c", content_hash="abc123...")

# 查询：传 workspace_instance_id（注册时拿到的 token），不传 workspace_root
client.get_callers(
    workspace_instance_id="ws_abc123...",
    relative_path="src/main.c",
)

# daemon 内部 6 步校验：owner / 规范化 / realpath / 前缀 / symlink / stat owner
```

---

## 当前架构保留（向后兼容）

**关键修正**：原方案"`cw --refresh-all` 不连接 Daemon，直接本地解析"在企业部署下会绕过全局 CAS、重新制造重复数据库、踩踏 I/O。**不能让 refresh 默认走单机**。改为三种显式模式：

**1. enterprise 模式（默认，当 daemon 可用时）**：
- `cw refresh --all` 检测 UDS 可用 + `enterprise_mode = true` → 走 daemon
- 所有解析、CAS 查询、thin DB 写入都通过 daemon 完成
- 客户端不持有 SQLite，只发请求

**2. local 模式（显式回退）**：
- `cw --local refresh --all` 或 `CW_MODE=local cw refresh --all`
- 不连 daemon，本地解析，本地 SQLite（`$HOME/.callwarden/<hash>/callwarden.db`）
- 仅用于：daemon 不可用、调试 daemon 自身、个人开发者单机、CI/CD 无 daemon 环境

**3. auto 模式（容错）**：
- `cw refresh --all` 默认 `auto`：先尝试 daemon，连不上才回退单机
- 适合过渡期，生产稳定后建议改为 `enterprise` 强制走 daemon
- 回退时打印 WARNING，避免静默降级到单机模式

**配置**：

```ini
# /etc/callwarden/daemon.conf
[client]
mode = enterprise      # enterprise / local / auto
uds_path = /var/run/callwarden.sock
fallback_warning = true
```

**禁止的行为**：
- 企业部署下 `cw refresh` 默认走单机（会绕 CAS）
- 客户端在 `enterprise` 模式下直接打开 `$HOME/.callwarden/<hash>/callwarden.db`（应只通过 daemon 读写）

---

## 后续优先级

1. **已完成** P26：git-aware 项目边界（解决 749→749 精确识别）
2. **计划** Phase 1 + Phase 3A：Rust 多语言 parse + Local CAS parse cache（详见 [enterprise-phase1-phase3-detail.md v5](enterprise-phase1-phase3-detail.md)）
3. **计划** Phase 2：Daemon + UDS（多用户协同 + Job Dedup，Global CAS 在此阶段接入）
4. **计划** Phase 3B：workspace 查询路径迁移 + resolved edge store
5. **计划** Phase 6：工具链库 + 路径穿越（企业级完善）

> **注**：原"阶段 1/2/3"命名已废弃，改为 v5 设计文档的 Phase 1/2/3A/3B/6 命名。

每阶段独立可交付，每阶段都有明确的收益和验证标准。
