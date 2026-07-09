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
- Ubuntu 14.04 / 16.04 / 18.04 / 20.04 / 22.04 / 24.06 多容器并存
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
│  │  │  /var/lib/call_warden/global_cache.db (只读共享) │  │  │
│  │  │  主键：sha256(content + lang + parser_version)   │  │  │
│  │  │  存储：AST / 符号表 / 调用关系（内容寻址）         │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Layer 2: 工具链专属库 (System/Toolchain DB)      │  │  │
│  │  │  /var/lib/call_warden/toolchain.db (只读)         │  │  │
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

**位置**：`/var/lib/call_warden/global_cache.db`（宿主机共享目录，所有用户只读访问）

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

**存储内容**：
- `file_cache` 表：`hash_key → AST 序列化 / 符号表 / 解析时间`
- `symbol_cache` 表：`hash_key → symbol_id / symbol_name / symbol_type / location`
- 查询命中：输入文件算 hash → 查 `file_cache` → 直接返回符号表，跳过 tree-sitter parse

**收益**：相同 commit 的相同文件，10 个开发人员只需解析一次。

### Layer 2: 工具链专属库

**目标**：`/opt` 下的工具链永远不变，一次扫描终身受益。

**位置**：`/var/lib/call_warden/toolchain.db`

**特点**：
- 完全只读
- 不参与 CAS（不需要 hash，因为这些文件不会变）
- 通过 SQLite `ATTACH DATABASE` 挂载到瘦工作区
- 查询外部符号（如 `printf`、`std::vector`）时直接命中

**初始化**：管理员运行 `cw daemon init-toolchain --path /opt/toolchain_a` 一次性扫描。

### Layer 3: 瘦工作区索引

**目标**：每个开发者本地 `repo` 目录下的 SQLite 极小（几 MB）。

**位置**：`$HOME/.callwarden/<workspace_fingerprint>/thin.db`

**Workspace Fingerprint 设计**（关键工程细节）：

不能用 Workspace Root 路径做 fingerprint。原因：
- user1: `/home/user1/work/firmware` → fingerprint_X
- user2: `/data/docker_volumes/user2/work/firmware` → fingerprint_Y
- 两个用户其实 clone 了同一个 repo 的同一个分支，内容完全相同，但路径不同

正确做法：
```python
workspace_fingerprint = sha256(
    git_remote_url +     # github.com/foo/bar.git
    git_head_commit_sha  # 不同分支 commit 不同
)
```

这样两个用户的工作区如果内容相同，fingerprint 相同，CAS 全局共享。

**存储内容**：
- `file_path_map` 表：`relative_path → content_hash`（核心映射）
- `call_edges` 表：`caller_symbol_id → callee_symbol_id`（调用关系本身的归属）
- `workspace_meta` 表：`git_remote / git_head / workspace_root`

**关键设计选择：调用关系的边存在哪一层？**

边是"哪个符号调用了哪个符号"，这是工作区特定的（不同分支的调用关系可能不同）。三种方案：

| 方案 | 节点（符号）存储 | 边存储 | 优点 | 缺点 |
|------|---------------|-------|------|------|
| A | CAS 库 | CAS 库 | 完全共享 | 跨文件边归属复杂 |
| B | 瘦工作区 | 瘦工作区 | 简单 | 跨分支不共享 |
| C（推荐） | CAS 库 | 瘦工作区 | 节点共享 + 边归属清晰 | 边需引用两端 hash |

**方案 C 设计**：
- 节点（符号）存在 CAS 库（按文件 hash 索引）
- 边存在瘦工作区，引用两端的 symbol_id（symbol_id = `hash(file) + symbol_offset`）
- 同一文件在不同分支被修改时，符号重新解析（新 hash），但其他分支的旧符号仍在 CAS
- 跨分支共享：相同 hash 的文件，符号只解析一次

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

**通信机制**：Unix Domain Socket (UDS) 或本地 TCP

- UDS 路径：`/var/run/call_warden.sock`
- 本地 TCP（容器穿透场景）：`127.0.0.1:8080`
- 协议：JSON-RPC 2.0（与 MCP 兼容）

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

**客户端查询接口**：
```python
# 客户端发起查询时必须带上 workspace_root
client.get_callers(
    workspace_root="/home/user1/work/firmware",  # 客户端视角的绝对路径
    relative_path="src/main.c",                    # 相对路径
    symbol_name="main",
)
```

Daemon 内部：
1. 用 `workspace_root` 计算实际文件路径
2. 读取文件算 content_hash
3. 用 hash 查 CAS 库
4. 返回结果

这样无论开发者从哪个容器、哪种协议挂载进来，只要相对于 Repo 的路径是对的，就能准确命中。

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

不能一次推翻现有架构，因为：
- 单机开发者仍需要零依赖的本地工具
- 企业部署初期，Daemon 还没稳定时有 fallback

### 阶段 1：CAS 全局缓存（核心收益）

**目标**：抽取文件 hash 索引层，多分支共享 AST/符号。

**改动范围**：
- 新增 `db/db_cas.py`：CAS 存储层（file_hash → AST/symbols）
- 修改 `db_build.py`：解析文件前先查 CAS，命中则跳过 tree-sitter parse
- 配置项：`callwarden.cas.enabled = true`、`callwarden.cas.db_path = /var/lib/call_warden/global_cache.db`

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
- 权限隔离（user1 不能查询 user2 的工作区）

**实施工作量**：约 2-3 周

### 阶段 3：工具链库 + 路径穿越（企业级完善）

**目标**：`/opt` 工具链只读库 + 容器路径穿越支持。

**改动范围**：
- 新增 `cw daemon init-toolchain`：一次性扫描工具链
- 修改所有路径处理：绝对路径 → 相对路径 + workspace_root
- Workspace Fingerprint：`sha256(git_remote_url + git_head_commit_sha)`

**预期收益**：
- 工具链符号一次扫描终身受益
- 容器/SMB/VSCode 三种访问方式路径统一

**风险**：
- 路径改造影响面广，需要逐个 Mixin 修改
- Workspace Fingerprint 依赖 git，无 git 项目需要 fallback

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

### 2. Workspace Fingerprint 不能用路径

```python
# 错误：用路径做 fingerprint
fingerprint = sha256(workspace_root_path)  # 不同容器路径不同

# 正确：用 git 元信息
fingerprint = sha256(git_remote_url + git_head_commit_sha)
```

否则两个用户 clone 同一 repo 的同一分支，因路径不同无法共享 CAS。

### 3. Job Deduplication 而非请求队列

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

### 4. 内存索引 + 磁盘内容

- 内存：`_hash_index: Dict[hash, FileMeta]`（10M 文件 ≈ 1GB）
- 磁盘：SQLite 存完整 AST/符号表（10M 文件 ≈ 50GB）
- 查询：内存命中 → 回查 SQLite 取详情

### 5. 调用关系的边归属

- 节点（符号）→ CAS 库（按文件 hash 索引）
- 边（调用关系）→ 瘦工作区（边引用两端 symbol_id）
- symbol_id = `hash(file) + symbol_offset`

### 6. 路径全相对化

```python
# 存储时
db.insert_file(relative_path="src/main.c", content_hash="abc123...")

# 查询时
client.get_callers(
    workspace_root="/home/user1/work/firmware",  # 客户端视角
    relative_path="src/main.c",
)
```

---

## 当前架构保留（向后兼容）

阶段 1-3 完成后，单机模式仍保留：

- `cw --refresh-all` 不连接 Daemon，直接本地解析
- 数据库路径仍是 `$HOME/.callwarden/<hash>/callwarden.db`
- 适用于：个人开发者、CI/CD、无 Daemon 的环境

Daemon 模式作为"可选加速层"：
- 有 Daemon → 走 CAS + 多用户协同
- 无 Daemon → 走单机模式

---

## 后续优先级

1. **已完成** P26：git-aware 项目边界（解决 749→749 精确识别）
2. **计划** 阶段 1：CAS 全局缓存（核心收益，95% 重复解析消灭）
3. **计划** 阶段 2：Daemon + UDS（多用户协同 + Job Dedup）
4. **计划** 阶段 3：工具链库 + 路径穿越（企业级完善）

每阶段独立可交付，每阶段都有明确的收益和验证标准。
